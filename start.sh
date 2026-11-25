#!/usr/bin/env sh
# POE2 Price Checker for macOS / Linux: run  ./start.sh
set -e
cd "$(dirname "$0")"
export PYTHONUTF8=1
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
fi
exec .venv/bin/python web.py --open
