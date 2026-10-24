#!/usr/bin/env python3
"""Install the bundled Skill offline without replacing an existing installation."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def install(project: Path) -> Path:
    if sys.version_info < (3, 10):
        raise ValueError("需要 Python 3.10 或更高版本")
    package = Path(__file__).resolve().parent
    source = package / "shuzirenskil"
    project = project.expanduser().resolve()
    if not project.is_dir():
        raise ValueError(f"项目目录不存在：{project}")
    target = project / ".agents" / "skills" / "shuzirenskil"
    if target.exists() or target.is_symlink():
        raise ValueError(f"已有同名 Skill，未覆盖：{target}。请先备份旧版本，再安装。")
    manifest = json.loads((package / "MANIFEST.sha256.json").read_text(encoding="utf-8"))
    expected = manifest["files"]
    actual = {
        p.relative_to(package).as_posix()
        for p in source.rglob("*") if p.is_file()
        and "__pycache__" not in p.relative_to(source).parts
        and p.name != ".DS_Store" and p.suffix != ".pyc"
    }
    if not expected or actual != set(expected):
        raise ValueError("安装包文件不完整或含额外文件，请重新下载并完整解压")
    for relative, digest in expected.items():
        path = package / relative
        if not relative.startswith("shuzirenskil/") or source.resolve() not in path.resolve().parents:
            raise ValueError("安装包文件路径无效")
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"文件校验失败：{relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Validate in a temporary directory, so failed installs leave no partial Skill.
    with tempfile.TemporaryDirectory(prefix=".shuzirenskil-install-", dir=target.parent) as temporary:
        staged = Path(temporary) / "shuzirenskil"
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        subprocess.run([sys.executable, "-B", str(staged / "scripts" / "validate_skill.py")],
                       check=True, env=env)
        if target.exists() or target.is_symlink():
            raise ValueError("安装期间目标目录已被创建，停止以避免覆盖")
        staged.rename(target)
    print(f"安装完成：{target}")
    print(f"技能文件版本：{manifest.get('version','未标记')}")
    print("请在该项目中新开 Codex 会话，使用 $shuzirenskil。")
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        print("制作视频前还需安装并配置到 PATH：" + "、".join(missing))
    print("技能文件复制未调用付费接口；如启用环境准备，后续会联网下载公开工具和模型。视频生成需要你自己的账号和配置。")
    print("下一步：请让 Codex 读取 Skill 的 references/setup-and-troubleshooting.md，完成系统凭据库保存及回读检查。")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="离线安装数字人口播 Skill，不覆盖已有版本")
    parser.add_argument("project", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument('--with-runtime', action='store_true', help='联网安装隔离依赖和识别模型，并运行实际自检')
    args = parser.parse_args()
    target = None
    try:
        target=install(args.project)
        if args.with_runtime:
            subprocess.run([sys.executable,'-B',str(target/'scripts/setup_environment.py')],check=True)
            subprocess.run([sys.executable,'-B',str(target/'scripts/run.py'),'doctor','--self-test'],check=True)
        else:
            print('这里只完成技能文件安装。自动准备完整本地依赖请加 --with-runtime；不能将文件复制成功当作语音环境已就绪。')
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"安装未完成：{exc}", file=sys.stderr)
        if target is not None:
            print(f"技能文件已经安装。请继续运行 {target / 'scripts/setup_environment.py'}，成功后运行 scripts/run.py doctor --self-test；不要覆盖重装。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
