"""Explicit one-time online dependency setup; no media uploads or paid calls."""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from _common import atomic_write_json, project_lock, sha256_file
from runtime_tools import runtime_home

MODEL_REPO = 'Systran/faster-whisper-small'
MODEL_REVISION = '536b0662742c02347bc0e980a01041f333bce120'
UV_VERSION = '0.9.5'


def run(args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def setup(skip_model=False, offline_cache=None):
    root = runtime_home()
    root.mkdir(parents=True, exist_ok=True)
    with project_lock(root):
        env_dir = root/'python-env'
        python = env_dir/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.is_file():
            if sys.version_info[:2] == (3, 12):
                run([sys.executable, '-m', 'venv', env_dir])
            else:
                if offline_cache:
                    raise ValueError('离线安装需已有 Python 3.12')
                # Keep the bootstrap tool private; never alter system Python.
                bootstrap = root/'bootstrap'
                run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                     '--target', bootstrap, f'uv=={UV_VERSION}'])
                env = os.environ.copy()
                env['PYTHONPATH'] = str(bootstrap)
                run([sys.executable, '-m', 'uv', 'venv', '--seed', '--python', '3.12', env_dir], env=env)
        requirement = Path(__file__).resolve().parents[1]/'requirements-runtime.txt'
        args = [python, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', requirement]
        cache = Path(offline_cache).resolve() if offline_cache else None
        if cache:
            verify_cache(cache, python)
            args += ['--no-index', '--find-links', cache/'wheels']
        run(args)
        # Catch platform-specific DLL/import failures before the large model download.
        run([python, '-c', 'import faster_whisper, opencc; from faster_whisper.vad import get_vad_model; from rapidocr_onnxruntime import RapidOCR; get_vad_model(); RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)'])
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not (ffmpeg and ffprobe):
            if cache:
                # Binary redistribution is separate from Python wheels; never pretend otherwise.
                raise ValueError('此依赖缓存不含 FFmpeg；请先安装 FFmpeg/ffprobe，或使用联网安装')
            result_file = root/'ffmpeg-paths.json'
            code = ('import json; from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise; '
                    'from pathlib import Path; import sys; '
                    'Path(sys.argv[1]).write_text(json.dumps(get_or_fetch_platform_executables_else_raise()))')
            run([python, '-c', code, result_file])
            ffmpeg, ffprobe = json.loads(result_file.read_text(encoding="utf-8"))
        model = root/'models/faster-whisper-small'
        if not skip_model:
            if cache:
                shutil.copytree(cache/'model', model, dirs_exist_ok=True)
            else:
                code = ('from huggingface_hub import snapshot_download; import sys; '
                        'snapshot_download(repo_id=sys.argv[1],revision=sys.argv[2],local_dir=sys.argv[3],'
                        'allow_patterns=["config.json","model.bin","tokenizer.json","vocabulary.txt","preprocessor_config.json","README.md"])')
                env = os.environ.copy()
                env['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
                run([python, '-c', code, MODEL_REPO, MODEL_REVISION, model], env=env)
        for executable in (ffmpeg, ffprobe):
            run([executable, '-version'], stdout=subprocess.DEVNULL)
        config = {'schema_version': '1.0', 'python': str(python.resolve()), 'ffmpeg': ffmpeg,
                  'ffprobe': ffprobe, 'model': str(model) if (model/'model.bin').is_file() else None,
                  'model_repository': MODEL_REPO, 'model_revision': MODEL_REVISION,
                  'platform': platform.system(), 'machine': platform.machine(),
                  'requirements_sha256': sha256_file(requirement), 'paid_requests': 0,
                  'agent_audiovisual_verified': False}
        # Do not resolve a venv Python symlink: its path selects the isolated environment.
        config['python'] = str(python.absolute())
        atomic_write_json(root/'runtime.json', config)
        freeze = subprocess.check_output([python, '-m', 'pip', 'freeze'], text=True, encoding="utf-8")
        (root/'installed-requirements.txt').write_text(freeze, encoding='utf-8')
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return config


def verify_cache(cache, python=None):
    manifest = json.loads((cache/'CACHE.json').read_text(encoding='utf-8'))
    actual={p.relative_to(cache).as_posix() for p in cache.rglob('*') if p.is_file() and p != cache/'CACHE.json'}
    if not manifest.get('files') or actual != set(manifest['files']) or manifest.get('model_revision') != MODEL_REVISION:
        raise ValueError('缓存文件清单或识别模型版本不匹配')
    if (manifest['platform'], manifest['machine'], manifest['python']) != (platform.system(), platform.machine(), '3.12'):
        raise ValueError('缓存仅适用于标注的系统、CPU 和 Python 3.12；不要跨系统复用')
    if python and subprocess.check_output([str(python),'-c','import sys; print("%d.%d"%sys.version_info[:2])'],text=True, encoding="utf-8").strip() != manifest['python']:
        raise ValueError('运行环境 Python 版本与依赖缓存不符')
    for relative, digest in manifest['files'].items():
        path = cache/relative
        if not path.resolve().is_relative_to(cache.resolve()) or path.is_symlink() or sha256_file(path) != digest:
            raise ValueError('依赖缓存文件损坏或路径非法')


def export_cache(destination):
    """A portable wheel/model cache, not a copied venv or a full OS distribution."""
    root = runtime_home()
    config = json.loads((root/'runtime.json').read_text(encoding='utf-8'))
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    version = subprocess.check_output([config['python'], '-c', 'import sys;print("%d.%d"%sys.version_info[:2])'], text=True, encoding="utf-8").strip()
    if version != '3.12':
        raise ValueError('当前导出缓存仅支持 Python 3.12；使用该版本创建环境后再导出')
    run([config['python'], '-m', 'pip', 'download', '--only-binary=:all:',
         '-r', root/'installed-requirements.txt', '-d', destination/'wheels'])
    shutil.copytree(config['model'], destination/'model', ignore=shutil.ignore_patterns('.cache'))
    shutil.copy2(root/'installed-requirements.txt', destination/'requirements-resolved.txt')
    manifest = {'platform': platform.system(), 'machine': platform.machine(), 'python': version,
                'model_revision': MODEL_REVISION, 'includes_ffmpeg': False, 'includes_python': False,
                'files': {p.relative_to(destination).as_posix(): sha256_file(p) for p in destination.rglob('*') if p.is_file()}}
    atomic_write_json(destination/'CACHE.json', manifest)
    print('已导出依赖和识别模型缓存；需已有匹配的 Python 3.12、FFmpeg/ffprobe。')


def main():
    parser = argparse.ArgumentParser(description='安装独立本地媒体/语音环境；首次需联网下载，不上传素材')
    parser.add_argument('--skip-model', action='store_true', help='仅技术环境，不宣称语音识别就绪')
    parser.add_argument('--offline-cache', type=Path)
    parser.add_argument('--export-cache', type=Path)
    args = parser.parse_args()
    try:
        if args.export_cache:
            export_cache(args.export_cache)
        else:
            setup(args.skip_model, args.offline_cache)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('环境准备未完成：'+str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
