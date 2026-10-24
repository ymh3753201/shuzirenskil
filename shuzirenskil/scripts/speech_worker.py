#!/usr/bin/env python3
"""Optional offline CosyVoice and WhisperX workers. No weight/dependency downloads."""
from __future__ import annotations
import argparse
import math
import os
import re
import socket
import sys
import wave
from pathlib import Path
from _common import atomic_write_json, read_json, sha256_file
from audio_timeline import master_info


def offline():
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    os.environ['MODELSCOPE_OFFLINE']='1'
    def denied(*args,**kwargs):raise RuntimeError('离线声音工作器禁止联网；请单独部署依赖与权重')
    socket.socket.connect=denied;socket.create_connection=denied


def tts(spec):
    if spec.get('voice_rights_confirmed') is not True:raise ValueError('先确认参考声音使用权')
    repo=Path(spec['repo']).resolve();model_path=Path(spec['model_dir']).resolve();dest=Path(spec['output']).resolve()
    if not (repo/'cosyvoice/cli/cosyvoice.py').is_file() or not model_path.is_dir():raise ValueError('请先部署 CosyVoice 源码及本地模型目录')
    if dest.exists():raise ValueError('输出已存在，不覆盖已确认音轨')
    script=Path(spec['script_file']).read_text(encoding="utf-8").strip()
    if not script:raise ValueError('口播稿为空')
    offline();sys.path[:0]=[str(repo),str(repo/'third_party/Matcha-TTS')]
    from cosyvoice.cli.cosyvoice import AutoModel
    model=AutoModel(model_dir=str(model_path))
    mode=spec.get('mode','reference')
    if mode=='reference':
        reference=Path(spec['reference_audio']).resolve()
        if not reference.is_file() or not spec.get('reference_transcript'):raise ValueError('参考声音需要文件及准确参考台词')
        prompt=spec['reference_transcript']
        if spec.get('cosyvoice_version',3)==3:prompt='You are a helpful assistant.<|endofprompt|>'+prompt
        iterator=model.inference_zero_shot(script,prompt,str(reference),stream=False)
    elif mode=='preset':
        speaker=spec['speaker']
        if speaker not in model.list_available_spks():raise ValueError('当前本地模型没有这个预设音色')
        iterator=model.inference_sft(script,speaker,stream=False)
    else:raise ValueError('mode 仅支持 reference 或 preset')
    if model.sample_rate not in {16000,24000,44100,48000}:raise ValueError('输出采样率需要显式转换后再进入严格音轨流程')
    dest.parent.mkdir(parents=True,exist_ok=True);temporary=dest.with_suffix('.partial.wav');count=0
    with wave.open(str(temporary),'wb') as writer:
        writer.setnchannels(1);writer.setsampwidth(2);writer.setframerate(model.sample_rate)
        for part in iterator:
            audio=part['tts_speech'].detach().cpu()
            if audio.ndim!=2 or audio.shape[0]!=1:raise ValueError('TTS 输出必须为单声道')
            raw=(audio[0].clamp(-1,1)*32767).to(dtype=__import__('torch').int16).numpy().astype('<i2').tobytes()
            writer.writeframesraw(raw);count+=len(raw)//2
    if not count:raise ValueError('TTS 没有输出声音')
    temporary.replace(dest)
    result={'status':'awaiting_audio_listening_review','master':master_info(dest),'script_sha256':sha256_file(Path(spec['script_file'])),
            'model_dir':str(model_path),'voice_mode':mode,'voice_quality_accepted':False}
    atomic_write_json(dest.with_suffix('.json'),result);return result


def alignment_to_cues(result,script):
    chars=[c for segment in result['segments'] for c in (segment.get('chars') or []) if not c['char'].isspace()]
    text=re.sub(r'\s+','',script)
    if ''.join(c['char'] for c in chars)!=text:raise ValueError('对齐结果缺字或重字；请修正粗时间段，不能猜测字幕')
    from planner import split_sentences
    sentences=split_sentences(text)
    cues=[];offset=0
    for sentence in sentences:
        group=chars[offset:offset+len(sentence)];offset+=len(sentence)
        timed=[c for c in group if type(c.get('start')) in (int,float) and type(c.get('end')) in (int,float) and math.isfinite(c['start']) and math.isfinite(c['end'])]
        if not timed:raise ValueError('某个句子没有实际对齐时间，需要人工修正')
        start=min(c['start'] for c in timed);end=max(c['end'] for c in timed)
        if not start<end or (cues and start<cues[-1]['end_seconds']):raise ValueError('对齐句子出现重叠，请校正')
        cues.append({'start_seconds':start,'end_seconds':end,'text':sentence})
    return cues


def align(spec):
    audio=Path(spec['audio']).resolve();master=master_info(audio);model_path=Path(spec['model_dir']).resolve()
    if not model_path.is_dir():raise ValueError('必须提供已部署的本地 WhisperX 对齐模型目录')
    script=Path(spec['script_file']).read_text(encoding="utf-8").strip();out=Path(spec['output'])
    if out.exists():raise ValueError('输出已存在，请保留旧时间码')
    blocks=spec.get('blocks')
    if blocks is None:
        if master['duration_seconds']>60:raise ValueError('长音轨需提供每段不超过 60 秒的粗时间块 blocks，文字必须完整覆盖确认稿')
        blocks=[{'start':0,'end':master['duration_seconds'],'text':script}]
    cursor=0
    for block in blocks:
        if not cursor<=block['start']<block['end']<=master['duration_seconds'] or block['end']-block['start']>60:raise ValueError('粗时间块重叠、越界或超过 60 秒')
        cursor=block['end']
    if re.sub(r'\s+','',''.join(b['text'] for b in blocks))!=re.sub(r'\s+','',script):raise ValueError('粗时间块台词不完整')
    offline()
    import nltk
    def no_download(*args,**kwargs):raise ValueError('缺少本地 NLTK 数据，请在部署时安装 punkt_tab')
    nltk.download=no_download
    import whisperx
    model,metadata=whisperx.load_align_model(language_code=spec.get('language','zh'),device=spec.get('device','cpu'),model_name=str(model_path),model_cache_only=True)
    result=whisperx.align(blocks,model,metadata,str(audio),spec.get('device','cpu'),return_char_alignments=True)
    cues=alignment_to_cues(result,script)
    candidate={'audio_sha256':master['sha256'],'alignment_reviewed':False,'cues':cues,
               'note':'算法对齐候选，必须按实际音频校正并听审；不能自动将 alignment_reviewed 改为 true'}
    atomic_write_json(out,candidate);return candidate


def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['tts','align']);parser.add_argument('--spec',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=(tts if args.command=='tts' else align)(read_json(args.spec))
        import json
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,ImportError,RuntimeError,KeyError) as exc:
        print('ERROR: '+str(exc),file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
