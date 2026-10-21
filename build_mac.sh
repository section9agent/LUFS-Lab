#!/usr/bin/env bash
# Builds "LUFS Lab.app" for macOS. Run this ON a Mac.
#   Output: dist/LUFS Lab.app   and   dist/LUFS-Lab-mac-<arch>.zip
# The app runs on the kind of Mac you build it on (Apple Silicon or Intel).
cd "$(dirname "$0")" || exit 1
. ./_find_python.sh

echo "Building LUFS Lab.app - this takes a few minutes..."
[ -x .buildvenv/bin/python ] || "$PY" -m venv .buildvenv || { echo "Could not create build environment."; exit 1; }
.buildvenv/bin/python -m pip install --quiet -r requirements.txt pyinstaller || { echo "Package install failed."; exit 1; }
.buildvenv/bin/python -m PyInstaller --noconfirm --clean --windowed --name "LUFS Lab" --icon icon.icns \
  --osx-bundle-identifier com.lufslab.app --collect-all soundfile --collect-all tkinterdnd2 \
  --hidden-import lameenc lufs_lab.py || { echo "Build failed - see messages above."; exit 1; }
ARCH="$(uname -m)"
ditto -c -k --keepParent "dist/LUFS Lab.app" "dist/LUFS-Lab-mac-$ARCH.zip"
echo
echo "Built: $PWD/dist/LUFS Lab.app"
echo "Zip for sharing: $PWD/dist/LUFS-Lab-mac-$ARCH.zip"
echo "First launch: the app is not signed, so right-click it > Open > Open (once)."
