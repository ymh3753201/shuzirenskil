"""Use the isolated runtime without activation commands or global PATH changes."""
import os
import subprocess
import sys
from pathlib import Path
from runtime_tools import settings, activate


def main():
    if len(sys.argv) < 2:
        raise ValueError('用法：python scripts/run.py doctor|studio|workflow|audio_qc|credential_store [参数]')
    name = sys.argv[1].removesuffix('.py')
    allowed = {'doctor', 'studio', 'workflow', 'audio_qc', 'credential_store', 'production_plan'}
    if name not in allowed:
        raise ValueError('不支持此命令')
    activate()
    python = settings().get('python') or sys.executable
    return subprocess.call([python, str(Path(__file__).with_name(name+'.py')), *sys.argv[2:]], env=os.environ.copy())


if __name__ == '__main__':
    raise SystemExit(main())
