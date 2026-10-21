#!/usr/bin/env bash
# Adds LUFS Lab to your Linux applications menu.
#   ./install_linux_launcher.sh                       uses run.sh in this folder
#   ./install_linux_launcher.sh /path/to/LUFS-Lab-x86_64.AppImage     uses a built program instead
HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-$HERE/run.sh}"
[ -e "$TARGET" ] || { echo "Not found: $TARGET"; exit 1; }
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/lufs-lab.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=LUFS Lab
Comment=Loudness leveling for music and vocals
Exec="$TARGET"
Icon=$HERE/icon.png
Terminal=false
Categories=AudioVideo;Audio;
DESK
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "Added LUFS Lab to your applications menu."
