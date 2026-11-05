#!/usr/bin/env bash
# FABLES Trading Lab — запуск на Linux/macOS (основная платформа — Windows: start.bat)
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
exec .venv/bin/python -m tradinglab serve "$@"
