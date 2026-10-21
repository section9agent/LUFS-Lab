#!/usr/bin/env bash
# Builds a stand-alone Linux program (and an AppImage) - no Python needed to run the result.
#   Output: dist/LUFS-Lab            single file
#           dist/LUFS-Lab-x86_64.AppImage   (or -aarch64) single file, runs on most distros
# TIP: the result runs on distributions as new as, or newer than, the one you build on -
#      build on an older distro (e.g. Ubuntu 22.04) for the widest reach.
cd "$(dirname "$0")" || exit 1
. ./_find_python.sh

echo "Building LUFS Lab for Linux - this takes a few minutes..."
[ -x .buildvenv/bin/python ] || "$PY" -m venv .buildvenv || { echo "Could not create build environment (Debian/Ubuntu: sudo apt install python3-venv)"; exit 1; }
.buildvenv/bin/python -m pip install --quiet -r requirements.txt pyinstaller || { echo "Package install failed."; exit 1; }
.buildvenv/bin/python -m PyInstaller --noconfirm --clean --onefile --windowed --name LUFS-Lab \
  --collect-all soundfile --collect-all tkinterdnd2 --hidden-import lameenc lufs_lab.py || { echo "Build failed - see messages above."; exit 1; }
echo "Built: $PWD/dist/LUFS-Lab"

# ---- AppImage (the 'universal' single-file Linux format) ----
ARCH="$(uname -m)"
TOOL="$PWD/.buildvenv/appimagetool-$ARCH.AppImage"
if [ ! -x "$TOOL" ]; then
  echo "Downloading appimagetool..."
  curl -fsSL -o "$TOOL" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage" \
    && chmod +x "$TOOL" || { echo "Could not download appimagetool - skipping the AppImage (dist/LUFS-Lab still works)."; exit 0; }
fi
rm -rf AppDir && mkdir -p AppDir/usr/bin
cp dist/LUFS-Lab AppDir/usr/bin/LUFS-Lab
cp icon.png AppDir/lufs-lab.png
cat > AppDir/lufs-lab.desktop <<DESK
[Desktop Entry]
Type=Application
Name=LUFS Lab
Comment=Loudness leveling for music and vocals
Exec=LUFS-Lab
Icon=lufs-lab
Terminal=false
Categories=AudioVideo;Audio;
DESK
printf '#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\nexec "$HERE/usr/bin/LUFS-Lab" "$@"\n' > AppDir/AppRun
chmod +x AppDir/AppRun
ARCH="$ARCH" "$TOOL" --appimage-extract-and-run AppDir "dist/LUFS-Lab-$ARCH.AppImage" >/dev/null 2>&1 \
  && echo "Built: $PWD/dist/LUFS-Lab-$ARCH.AppImage" || echo "AppImage step failed - dist/LUFS-Lab still works."
rm -rf AppDir
