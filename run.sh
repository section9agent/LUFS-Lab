#!/usr/bin/env bash
# LUFS Lab launcher for Linux and macOS.
#   ./run.sh                      opens the window
#   ./run.sh song.wav --match     command line (see README)
cd "$(dirname "$0")" || exit 1

pause_fail() { echo; echo "$1"; echo; if [ -t 0 ]; then read -r -p "Press Enter to close..." _; fi; exit 1; }

. ./_find_python.sh

if [ ! -x .venv/bin/python ]; then
  echo "Setting up LUFS Lab (first run only)..."
  "$PY" -m venv .venv || pause_fail "Could not create the Python environment. On Debian/Ubuntu run:  sudo apt install python3-venv"
fi

STAMP="$(cksum < requirements.txt | tr -d ' ')"
if [ ! -f .venv/.stamp ] || [ "$(cat .venv/.stamp)" != "$STAMP" ]; then
  echo "Installing required packages (first run takes a minute)..."
  .venv/bin/python -m pip install --quiet -r requirements.txt \
    || pause_fail "Package install failed. Check your internet connection and try again."
  echo "$STAMP" > .venv/.stamp
fi

exec .venv/bin/python lufs_lab.py "$@"
