"""Local, evidence-producing audio checks, independent of subtitle delivery."""
import argparse
import difflib
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path
from _common import atomic_write_json, sha256_file, digest_json
from media import detect_tail_silence, analyze_speech_pacing, technical_summary, require_command
from runtime_tools import settings


def normalized(text):
    return re.sub(r'[\W_]+', '', text, flags=re.UNICODE).lower()


def evidence_valid(report, video, script):
    return (report.get('schema_version') == 'audio-qc-1.0'
            and report.get('video_sha256') == sha256_file(Path(video))
            and report.get('script_sha256') == digest_json(script)
            and report.get('recognition', {}).get('status') == 'completed')


def inspect_audio(video, output, script='', language=None, *, transcribe=True):
    video, output = Path(video).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    summary = technical_summary(video)
    report = {'schema_version': 'audio-qc-1.0', 'video_sha256': sha256_file(video),
              'script_sha256': digest_json(script), 'video': str(video), 'technical': summary,
              'recognition': {'status': 'unavailable'}, 'perceptual_review': 'pending',
              'network_calls': 0, 'status': 'partial', 'paid_requests': 0}
    if not summary['has_audio']:
        report.update(status='blocked', reason='没有音轨，不能进行口播质检')
        atomic_write_json(output/'audio-qc.json', report)
        return report
    # Validate audio decoding independently of transcript availability.
    subprocess.run([require_command('ffmpeg'), '-v', 'error', '-xerror', '-i', str(video),
                    '-map', '0:a:0', '-f', 'null', '-'], check=True, capture_output=True)
    report['silence'] = detect_tail_silence(video)
    report['pacing'] = analyze_speech_pacing(video, script, 260)
    config = settings()
    python, model = config.get('python'), config.get('model')
    if transcribe and python and model and Path(python).is_file() and Path(model, 'model.bin').is_file():
        with tempfile.TemporaryDirectory(prefix='speech-qc-') as temp:
            raw = Path(temp)/'recognition.json'
            command = [python, str(Path(__file__).with_name('audio_worker.py')), '--audio', str(video),
                       '--model', model, '--output', str(raw)]
            if language:
                command += ['--language', language]
            try:
                subprocess.run(command, check=True, capture_output=True, timeout=1800)
                recognition = json.loads(raw.read_text(encoding='utf-8'))
                recognition['status'] = 'completed'
                report['recognition'] = recognition
                text = recognition['transcript']
                report['script_matches'] = normalized(text) == normalized(script) if script else None
                report['script_diff'] = list(difflib.ndiff(list(normalized(script)), list(normalized(text)))) if script else []
                (output/'transcript.txt').write_text(text, encoding='utf-8')
                report['status'] = 'automatic_checks_completed'
            except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
                diagnostic=getattr(exc,'stderr',None) or str(exc)
                if isinstance(diagnostic,bytes):diagnostic=diagnostic.decode('utf-8',errors='replace')
                report['recognition'] = {'status': 'failed', 'reason': '本地识别失败；检查 runtime/doctor，不能以确认稿代替识别',
                                         'error_type':type(exc).__name__,'diagnostic':diagnostic[-2000:]}
    atomic_write_json(output/'audio-qc.json', report)
    return report


def tail_decision(video, script='', cues=None, report=None, preserve=False):
    """Conservative ending only. Cues are reviewed; ASR/VAD never imply subjective acceptance."""
    video = Path(video)
    summary = technical_summary(video)
    duration = summary['duration_seconds']
    result = {'policy': 'tail-v1', 'source_sha256': sha256_file(video), 'original_seconds': duration,
              'end_seconds': duration, 'trim': False, 'reason': 'no_excess_tail', 'keep_seconds': 0.3}
    if preserve:
        return {**result, 'reason': 'intentional_hold_or_locked_audio'}
    if not summary['has_audio']:
        raise ValueError('口播视频没有音轨，不能自动裁成合格片段')
    silence = detect_tail_silence(video)
    if silence['tail_silence_seconds'] >= duration - 0.2:
        raise ValueError('整段基本静音，需检查实际口播，不能靠裁剪放行')
    cue_end = max((cue['end_seconds'] for cue in cues or []), default=0)
    if not math.isfinite(cue_end) or not 0 <= cue_end <= duration + 0.05:
        raise ValueError('已审语音时间码越界，不能决定裁尾')
    end, reason = duration, 'no_excess_tail'
    if silence['has_excess_tail_silence']:
        end = max(silence['last_silence_start_seconds'], cue_end) + 0.3
        reason = 'trailing_silence_with_reviewed_cue_guard'
    if report and evidence_valid(report, video, script):
        recognition = report['recognition']
        regions, words = recognition.get('speech_regions', []), recognition.get('words', [])
        exact = normalized(recognition.get('transcript', '')) == normalized(script) and bool(normalized(script))
        if exact and regions and words and cues:
            for interval in [*regions,*words]:
                start,stop=interval.get('start_seconds'),interval.get('end_seconds')
                if (type(start) not in (int,float) or type(stop) not in (int,float)
                    or not math.isfinite(start) or not math.isfinite(stop) or not 0<=start<stop<=duration+0.05):
                    raise ValueError('识别时间码无效，不能用作裁尾依据')
            asr_end = max(word['end_seconds'] for word in words)
            vad_end = max(region['end_seconds'] for region in regions)
            # Disagreement, weak endings or missing words never justify cutting sound.
            strong = all(word.get('probability', 0) >= 0.7 for word in words[-3:])
            if strong and max(asr_end, vad_end, cue_end) - min(asr_end, vad_end, cue_end) <= 0.5:
                candidate = max(asr_end, vad_end, cue_end) + 0.3
                if 0.2 < candidate < end:
                    end, reason = candidate, 'reviewed_cues_plus_exact_asr_and_vad'
    if duration - end > 0.3:
        result.update(trim=True, end_seconds=min(duration, end), reason=reason)
    return result


def main():
    parser = argparse.ArgumentParser(description='免费本地音频质检；不上传、不生成、不伪造听审')
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--script-file', type=Path)
    parser.add_argument('--language')
    parser.add_argument('--no-asr', action='store_true')
    args = parser.parse_args()
    script = args.script_file.read_text(encoding='utf-8-sig') if args.script_file else ''
    result = inspect_audio(args.video, args.output, script, args.language, transcribe=not args.no_asr)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'automatic_checks_completed' else (1 if result['status'] == 'blocked' else 2)


if __name__ == '__main__':
    raise SystemExit(main())
