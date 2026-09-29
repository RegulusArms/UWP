#!/bin/sh
# Installs UWP for the current user (no root needed once dependencies exist).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"

DEPS="python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 \
gir1.2-ayatanaappindicator3-0.1 gir1.2-wnck-3.0 gstreamer1.0-gl \
gstreamer1.0-gtk3 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
gstreamer1.0-plugins-bad gstreamer1.0-libav ffmpeg \
gnome-shell-extension-appindicator"
MISSING=""
for p in $DEPS; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
done
if [ -n "$MISSING" ]; then
    echo "Installing missing packages:$MISSING"
    sudo apt-get install -y $MISSING
fi

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" \
         "$HOME/.local/share/icons/hicolor/scalable/apps"
ln -sf "$HERE/bin/uwp" "$HOME/.local/bin/uwp"
cp "$HERE/uwp/icons/io.github.RegulusArms.UWP.svg" \
   "$HOME/.local/share/icons/hicolor/scalable/apps/"
cat > "$HOME/.local/share/applications/io.github.RegulusArms.UWP.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=UWP Wallpapers
Comment=Lightweight image and video wallpapers
Exec=$HERE/bin/uwp --open
Icon=io.github.RegulusArms.UWP
Categories=Utility;GTK;
StartupNotify=false
DESKTOP

"$HERE/bin/uwp" --background >/dev/null 2>&1 &
echo "Installed. UWP is now in your top bar; also in the app grid as 'UWP Wallpapers'."
echo "Turn on 'Start UWP when I log in' in Shortcuts & settings to autostart it."
