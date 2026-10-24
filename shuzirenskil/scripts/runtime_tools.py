"""Discover the private, machine-local runtime. Never download during production."""
import json
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


def runtime_home():
    return Path(os.environ.get('SHUZIRENSKIL_RUNTIME_HOME', str(Path.home()/'.cache/shuzirenskil/runtime-v1'))).expanduser().resolve()


def settings():
    path = runtime_home()/'runtime.json'
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def command_path(name):
    configured = settings().get(name)
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    return shutil.which(name)


def activate():
    """Also cover legacy subprocess calls without changing the user's global PATH."""
    folders = []
    for name in ('ffmpeg', 'ffprobe'):
        value = settings().get(name)
        if value and Path(value).is_file():
            parent = str(Path(value).resolve().parent)
            if parent not in folders:
                folders.append(parent)
    if folders:
        os.environ['PATH'] = os.pathsep.join(folders + [os.environ.get('PATH', '')])


def probe_command(name):
    path = command_path(name)
    if not path:
        return {'status': 'missing', 'path': None}
    try:
        result = subprocess.run([path, '-version'], capture_output=True, text=True, encoding="utf-8", timeout=15)
        return {'status': 'available' if result.returncode == 0 else 'failed', 'path': path,
                'version': result.stdout.splitlines()[0] if result.stdout else ''}
    except (OSError, subprocess.SubprocessError):
        return {'status': 'failed', 'path': path}


def capabilities():
    config = settings()
    skill = Path(__file__).resolve().parents[1]
    requirement = skill/'requirements-runtime.txt'
    report = {'schema_version': '1.0', 'python': sys.version.split()[0],
              'runtime_home': str(runtime_home()),
              'ffmpeg': probe_command('ffmpeg'), 'ffprobe': probe_command('ffprobe'),
              'speech_recognition': {'status': 'missing'},
              'agent_audio_input': {'status': 'unverified', 'reason': '由本次会话实际工具确认，安装软件不能证明 AI 听过'},
              'agent_full_video_input': {'status': 'unverified', 'reason': '抽帧和打开播放器不等于 AI 已完整看听'},
              'real_media_quality_verified': False, 'network_calls': 0}
    report['skill_version']=(skill/'VERSION').read_text(encoding='utf-8').strip() if (skill/'VERSION').is_file() else 'legacy'
    report['runtime_requirements_match']=bool(config) and config.get('requirements_sha256') == hashlib.sha256(requirement.read_bytes()).hexdigest()
    python = config.get('python')
    model = config.get('model')
    if python and Path(python).is_file() and model and Path(model, 'model.bin').is_file():
        try:
            code = 'import faster_whisper, onnxruntime; from faster_whisper.vad import get_vad_model; get_vad_model(); print(faster_whisper.__version__)'
            result = subprocess.run([python, '-c', code], capture_output=True, text=True, encoding="utf-8", timeout=45)
            report['speech_recognition'] = {'status': 'available' if result.returncode == 0 else 'failed',
                'engine': 'faster-whisper', 'version': result.stdout.strip() if result.returncode == 0 else '',
                'model': model, 'model_load_verified': False,
                'note': '依赖/VAD 可加载，模型实际推理还需 doctor --self-test 验证'}
            if result.returncode:
                report['speech_recognition']['diagnostic']=result.stderr[-2000:]
        except (OSError, subprocess.SubprocessError):
            report['speech_recognition'] = {'status': 'failed', 'reason': '本地识别环境启动失败'}
    report['technical_ready'] = all(report[x]['status'] == 'available' for x in ('ffmpeg', 'ffprobe'))
    report['automatic_audio_ready'] = report['technical_ready'] and report['speech_recognition']['status'] == 'available' and report['runtime_requirements_match']
    return report
