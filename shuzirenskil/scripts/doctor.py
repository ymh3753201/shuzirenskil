"""Read-only dependency diagnostics; optional real offline ASR/decode smoke test."""
import argparse
import json
import subprocess
import tempfile
import wave
from pathlib import Path
from _common import atomic_write_json
from runtime_tools import capabilities, settings


def diagnose(self_test=False):
    report = capabilities()
    report['automatic_audio_dependencies_available'] = report['automatic_audio_ready']
    if self_test and report['automatic_audio_ready']:
        from audio_qc import inspect_audio
        with tempfile.TemporaryDirectory(prefix='skill-audio-selftest-') as temp:
            root = Path(temp)
            with wave.open(str(root/'silence.wav'), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
                stream.writeframes(b'\0'*(2*16000))
            result = inspect_audio(root/'silence.wav', root/'qa', transcribe=True)
            recognition = result['recognition']
            passed = recognition['status'] == 'completed' and not recognition.get('transcript') and not recognition.get('speech_regions')
            report['speech_self_test'] = {'passed': passed, 'fixture': '1 second silence; not a speech accuracy test'}
            report['speech_recognition']['model_load_verified'] = passed
            report['automatic_audio_ready'] = passed
    elif not self_test:
        report['automatic_audio_ready'] = False
    report['status'] = 'automatic_audio_ready' if report['automatic_audio_ready'] else ('technical_only' if report['technical_ready'] else 'missing_dependencies')
    report['next_action'] = ('请运行 doctor --self-test 验证实际模型推理；' if not self_test else '') + '由 AI 检查本会话实际视听工具；缺少的主观检查在制作前说明，不能伪造已听过'
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = diagnose(args.self_test)
    if args.output:
        atomic_write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['automatic_audio_ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
