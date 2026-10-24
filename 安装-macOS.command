#!/bin/zsh
set -eu
PACKAGE_DIR="${0:A:h}"
PROJECT_DIR="${1:-$PWD}"
if ! command -v python3 >/dev/null 2>&1; then
  echo "请先安装 Python 3.10 或更高版本，再运行安装程序。"
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
exec python3 -B "$PACKAGE_DIR/install.py" "$PROJECT_DIR" --with-runtime
