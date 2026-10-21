# Sourced by the other scripts. Sets PY to a Python 3.10+ that has tkinter, or prints help and exits.
PY=""; HAVE_OLD_ENOUGH=""
for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
  command -v "$c" >/dev/null 2>&1 || continue
  "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null || continue
  HAVE_OLD_ENOUGH=1
  if "$c" -c 'import tkinter' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo
  if [ -z "$HAVE_OLD_ENOUGH" ]; then
    echo "LUFS Lab needs Python 3.10 or newer, and it was not found."
    if [ "$(uname)" = "Darwin" ]; then
      echo "Install it from https://www.python.org/downloads/macos/ (that installer includes the window toolkit),"
      echo "or with Homebrew:  brew install python-tk"
    else
      echo "Install it with your package manager, e.g.  sudo apt install python3 python3-venv python3-tk"
    fi
  else
    echo "Python is installed, but its window toolkit (tkinter) is missing."
    if [ "$(uname)" = "Darwin" ]; then
      echo "Install Python from https://www.python.org/downloads/macos/ (includes it), or:  brew install python-tk"
    elif command -v apt-get >/dev/null 2>&1; then echo "Run:  sudo apt install python3-tk python3-venv"
    elif command -v dnf >/dev/null 2>&1; then echo "Run:  sudo dnf install python3-tkinter"
    elif command -v pacman >/dev/null 2>&1; then echo "Run:  sudo pacman -S tk"
    elif command -v zypper >/dev/null 2>&1; then echo "Run:  sudo zypper install python3-tk"
    else echo "Install your distribution's tkinter package (often named python3-tk or python3-tkinter)."
    fi
  fi
  echo
  if [ -t 0 ]; then read -r -p "Press Enter to close..." _; fi
  exit 1
fi
