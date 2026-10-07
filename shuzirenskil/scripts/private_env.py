#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path


KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _private_file(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if path.is_symlink() or not resolved.is_file():
        raise ValueError(f".env 必须是普通文件，不能是链接: {path}")
    if os.name != "nt":
        mode = stat.S_IMODE(resolved.stat().st_mode)
        if mode & 0o077:
            raise ValueError(f".env 权限过宽（当前 {mode:o}），请执行 chmod 600 '{resolved}'")
    return resolved


def parse_env_file(path: Path) -> dict[str, str]:
    resolved = _private_file(path)
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(resolved.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f".env 第 {line_number} 行缺少等号")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not KEY_RE.fullmatch(key):
            raise ValueError(f".env 第 {line_number} 行变量名无效")
        if key in values:
            raise ValueError(f".env 中变量重复: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if "$" in value or "`" in value:
            raise ValueError(f".env 变量 {key} 不允许命令或变量展开")
        values[key] = value
    return values


def _keychain_value(service: str) -> str:
    if sys.platform != "darwin" or not Path("/usr/bin/security").is_file():
        return ""
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def load_private_value(
    name: str,
    explicit_env_file: Path | None,
    project_dir: Path,
    *,
    keychain_service: str | None = None,
    fallback_names: tuple[str, ...] = (),
    prefer_keychain: bool = False,
) -> str:
    names = tuple(dict.fromkeys((name, *fallback_names)))

    def from_values(values: dict[str, str]) -> str:
        for candidate in names:
            value = values.get(candidate, "").strip()
            if value:
                return value
        return ""

    if explicit_env_file:
        value = from_values(parse_env_file(explicit_env_file))
        if value:
            return value
        if prefer_keychain and keychain_service:
            value = _keychain_value(keychain_service)
            if value:
                return value
        raise ValueError(f"私密配置文件中 {name} 及其备用变量为空: {explicit_env_file.resolve()}")

    if prefer_keychain and keychain_service:
        value = _keychain_value(keychain_service)
        if value:
            return value

    for candidate in names:
        existing = os.environ.get(candidate, "").strip()
        if existing:
            return existing

    candidates = [project_dir / ".env", Path.cwd() / ".env"]
    checked: set[Path] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        candidate = candidate.expanduser()
        resolved = candidate.resolve()
        if resolved in checked:
            continue
        checked.add(resolved)
        if not candidate.exists():
            if explicit_env_file:
                raise ValueError(f"指定的 .env 不存在: {candidate}")
            continue
        value = from_values(parse_env_file(candidate))
        if value:
            return value
        raise ValueError(f"私密配置文件中 {name} 及其备用变量为空: {resolved}")
    if not prefer_keychain and keychain_service:
        value = _keychain_value(keychain_service)
        if value:
            return value
    keychain_hint = f"、macOS 钥匙串服务 {keychain_service}" if keychain_service else ""
    raise ValueError(f"缺少私密环境变量 {name}；可放入权限为 600 的项目 .env{keychain_hint}")
