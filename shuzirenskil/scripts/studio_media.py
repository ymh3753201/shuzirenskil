"""Actual-frame timeline, explicit subtitle cues and full-master audio delivery."""
from __future__ import annotations
import hashlib
import math
import shutil
import subprocess
import wave
from fractions import Fraction
from pathlib import Path
from _common import atomic_write_json, sha256_file
from media import probe, validate_full_decode


def command(args):
    result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if result.returncode:raise ValueError('媒体处理失败：'+result.stderr.decode(errors='replace')[-1200:])
    return result.stdout


def info(path):
    data=probe(Path(path));video=next((s for s in data['streams'] if s['codec_type']=='video'),None)
    if not video:raise ValueError('文件没有可解码视频轨')
    fps=float(Fraction(video.get('avg_frame_rate','0/1')))
    seconds=float(video.get('duration') or data.get('format',{}).get('duration') or 0)
    if not fps or not math.isfinite(seconds) or seconds<=0:raise ValueError('媒体时间信息无效')
    return {'width':video['width'],'height':video['height'],'fps':fps,'fps_fraction':str(Fraction(video['avg_frame_rate'])),
            'seconds':seconds,'has_audio':any(s['codec_type']=='audio' for s in data['streams'])}


def check_clip(path, job, plan, config, *, lip=False):
    meta=info(path)
    width,height=config['expected_dimensions']
    if (meta['width'],meta['height']) != (width,height):raise ValueError('视频尺寸与确认的输出尺寸不符')
    expected_fps=config.get('expected_fps')
    if expected_fps and abs(meta['fps']-expected_fps)>0.05:raise ValueError('视频帧率与确认值不符')
    expected=job.get('use_seconds',job['request_seconds']) if lip else job['request_seconds']
    if abs(meta['seconds']-expected)>max(0.15,2/meta['fps']):raise ValueError('视频实际时长不符，不能自动拉伸或重复画面')
    if job['native_audio_policy']=='retain_and_review' and not meta['has_audio']:raise ValueError('原生口播片段没有音轨')
    # Full decode, rather than trusting only container metadata.
    validate_full_decode(Path(path), require_audio=job['native_audio_policy']=='retain_and_review')
    return meta


def context_clip(path, output, seconds=2, end_seconds=None):
    meta=info(path)
    end = meta['seconds'] if end_seconds is None else end_seconds
    if not math.isfinite(end) or not seconds <= end <= meta['seconds']+2/meta['fps']:raise ValueError('已审有效尾段不足 2 秒或越界，不能作为续接参考')
    end=min(meta['seconds'],math.ceil(end*meta['fps']-1e-7)/meta['fps'])
    command(['ffmpeg','-v','error','-y','-ss',str(end-seconds),'-i',str(path),'-t',str(seconds),
             '-an','-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart',str(output)])
    return {'path':str(output.resolve()),'sha256':sha256_file(output),'seconds':seconds,'source_end_seconds':end}


def stamp(seconds):
    ms=round(seconds*1000); h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def write_subtitles(cues, duration, expected_text, path):
    from production_plan import _normalized
    if not isinstance(cues,list) or not cues:raise ValueError('缺少实际音频上的字幕时间码')
    end=0; parts=[];texts=[]
    for index,cue in enumerate(cues,1):
        a,b=cue['start_seconds'],cue['end_seconds'];text=cue['text']
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (a,b)) or not end<=a<b<=duration+0.001:
            raise ValueError('字幕时间码有重叠、倒序或越界')
        if not isinstance(text,str) or not text.strip() or '\n' in text:raise ValueError('字幕文字无效')
        end=b;texts.append(text);parts.append(f'{index}\n{stamp(a)} --> {stamp(b)}\n{text}\n')
    if _normalized(''.join(texts))!=_normalized(expected_text):raise ValueError('字幕与确认稿不一致')
    Path(path).write_text('\n'.join(parts),encoding='utf-8')


def render(plan, clips, output, edits=None):
    """Render accepted clips; never trim master audio, stretch speech or fabricate frames."""
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    jobs=plan['jobs']
    if len(clips)!=len(jobs):raise ValueError('片段数量与计划不符')
    if edits is not None and len(edits)!=len(jobs):raise ValueError('裁尾记录数量与片段不符')
    first=info(clips[0]);fps=first['fps']; locked=bool(plan.get('audio_plan'))
    timeline=[];cursor_frames=0; files=[]
    for index,(job,path) in enumerate(zip(jobs,clips),1):
        meta=info(path)
        if (meta['width'],meta['height'],meta['fps_fraction'])!=(first['width'],first['height'],first['fps_fraction']):
            raise ValueError('片段尺寸或帧率不一致，请显式统一输出规格后重新验收')
        if locked:
            rate=plan['audio_plan']['master']['sample_rate']
            end_frame=round(job['end_sample']/rate*fps)
            frames=end_frame-cursor_frames
        else:
            # Count actual decoded frames; metadata duration can include an AAC tail.
            counted=command(['ffprobe','-v','error','-select_streams','v:0','-count_frames','-show_entries','stream=nb_read_frames','-of','csv=p=0',str(path)])
            frames=int(counted.strip())
            from audio_qc import tail_decision
            edit = edits[index-1] if edits is not None else tail_decision(path, preserve=job.get('kind','talking_head')!='talking_head' or job['native_audio_policy']!='retain_and_review')
            if edit.get('source_sha256') != sha256_file(Path(path)):
                raise ValueError('裁尾记录没有绑定当前原片')
            end = edit['end_seconds']
            if type(end) not in (int,float) or not math.isfinite(end) or not 0 < end <= meta['seconds']+0.05:
                raise ValueError('裁尾时间无效')
            # Ceil protects the last reviewed syllable; no frame interpolation.
            frames=min(frames, math.ceil(end*fps-1e-7))
        if frames<=0 or meta['seconds']+1/fps<frames/fps:raise ValueError('视频不足以覆盖主音轨，不能冻结补帧')
        target=output/f'part_{index:04d}.mkv'
        vf=f'trim=end_frame={frames},setpts=PTS-STARTPTS'
        args=['ffmpeg','-v','error','-y','-i',str(path)]
        if locked:
            args += ['-vf',vf,'-an','-c:v','libx264','-preset','fast','-crf','18']
        else:
            if job['native_audio_policy']=='discard' or not meta['has_audio']:
                args += ['-f','lavfi','-i','anullsrc=r=48000:cl=mono','-map','0:v:0','-map','1:a:0']
            else:args+=['-map','0:v:0','-map','0:a:0']
            args+=['-vf',vf,'-af',f'apad,atrim=duration={frames/fps},asetpts=PTS-STARTPTS','-c:v','libx264','-preset','fast','-crf','18','-c:a','pcm_s16le','-ar','48000','-ac','1']
        args+=['-t',str(frames/fps),str(target)];command(args);files.append(target)
        timeline.append({'job_id':job['job_id'],'start_seconds':cursor_frames/fps,'end_seconds':(cursor_frames+frames)/fps,
                         'frames':frames,'script':job['script'],'source_sha256':sha256_file(Path(path)),
                         'source_end_seconds':frames/fps,'tail_edit':None if locked else edit})
        cursor_frames+=frames
    listing=output/'concat.txt';listing.write_text(''.join("file '"+p.name+"'\n" for p in files))
    joined=output/'joined.mkv'
    command(['ffmpeg','-v','error','-y','-f','concat','-safe','1','-i',str(listing),'-c','copy',str(joined)])
    final=output/'final.mp4';master_output=None
    if locked:
        master=Path(plan['audio_plan']['master']['path'])
        if sha256_file(master)!=plan['audio_plan']['master']['sha256']:raise ValueError('主音轨已改变')
        master_output=output/'final.lossless-audio.mov'
        command(['ffmpeg','-v','error','-y','-i',str(joined),'-i',str(master),'-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','pcm_s16le',str(master_output)])
        # Verify every decoded sample against original WAV, not just duration.
        decoded_hash=hashlib.sha256();original_hash=hashlib.sha256()
        with subprocess.Popen(['ffmpeg','-v','error','-i',str(master_output),'-map','0:a:0','-f','s16le','-'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as process:
            while chunk:=process.stdout.read(1024*1024):decoded_hash.update(chunk)
            if process.wait()!=0:raise ValueError('无法解码无损主音轨')
        with wave.open(str(master),'rb') as src:
            while chunk:=src.readframes(48000*10):original_hash.update(chunk)
        if decoded_hash.digest()!=original_hash.digest():raise ValueError('无损成片音轨与主音轨不同')
        shutil.copyfile(master,output/'master.wav')
        command(['ffmpeg','-v','error','-y','-i',str(master_output),'-c:v','copy','-c:a','aac','-b:a','192k','-movflags','+faststart',str(final)])
    else:
        command(['ffmpeg','-v','error','-y','-i',str(joined),'-c:v','copy','-c:a','aac','-b:a','192k','-movflags','+faststart',str(final)])
    validate_full_decode(Path(final))
    actual=info(final)
    frame_count=int(command(['ffprobe','-v','error','-select_streams','v:0','-count_frames','-show_entries','stream=nb_read_frames','-of','csv=p=0',str(final)]).strip())
    if frame_count!=cursor_frames or abs(actual['seconds']-cursor_frames/fps)>2/fps:
        raise ValueError('最终实际帧数或时间与合成时间轴不符')
    result={'status':'awaiting_final_review','video':str(final.resolve()),'video_sha256':sha256_file(final),
            'timeline':timeline,'actual_video_seconds':actual['seconds'],'actual_video_frames':frame_count,'duration_seconds':cursor_frames/fps,'master_audio_preserved':locked,
            'lossless_audio_video':str(master_output.resolve()) if master_output else None,
            'mp4_audio_note':'AAC 交付版有编码损失；无损 MOV 与 master.wav 保留原始样本' if locked else '原生声音已重新编码为 AAC',
            'technical_decode_passed':True,'business_accepted':False}
    atomic_write_json(output/'render.json',result)
    return result
