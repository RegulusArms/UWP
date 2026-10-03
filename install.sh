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

# GNOME Shell helper extension: keeps the wallpaper under the desktop icons
# on Wayland and lets UWP see which monitors are covered there.
EXT_UUID=uwp-helper@regulusarms.github.io
EXT_NOTE=""
if command -v gnome-shell >/dev/null && [ -x /usr/bin/gsettings ]; then
    EXT_DIR="$HOME/.local/share/gnome-shell/extensions"
    mkdir -p "$EXT_DIR"
    ln -sfn "$HERE/gnome-extension/$EXT_UUID" "$EXT_DIR/$EXT_UUID"
    if ! gnome-extensions info "$EXT_UUID" 2>/dev/null | grep -q 'State: ACTIVE'
    then
        # gsettings, not "gnome-extensions enable": the Shell doesn't know a
        # new extension until it restarts, i.e. the next login on Wayland.
        /usr/bin/python3 - "$EXT_UUID" <<'PY'
import subprocess, sys, ast
uuid = sys.argv[1]
def get(key):
    out = subprocess.check_output(['/usr/bin/gsettings', 'get', 'org.gnome.shell', key],
                                  text=True).strip()
    return ast.literal_eval(out.removeprefix('@as').strip())
def put(key, val):
    subprocess.check_call(['/usr/bin/gsettings', 'set', 'org.gnome.shell', key, str(val)])
on = get('enabled-extensions')
if uuid not in on:
    put('enabled-extensions', on + [uuid])
off = get('disabled-extensions')
if uuid in off:
    put('disabled-extensions', [u for u in off if u != uuid])
PY
        gnome-extensions enable "$EXT_UUID" 2>/dev/null || true
        gnome-extensions info "$EXT_UUID" 2>/dev/null | grep -q 'State: ACTIVE' \
            || EXT_NOTE=yes
    fi
fi

"$HERE/bin/uwp" --background >/dev/null 2>&1 &
echo "Installed. UWP is now in your top bar; also in the app grid as 'UWP Wallpapers'."
if [ -n "$EXT_NOTE" ]; then
    echo "Log out and back in once to start the UWP helper GNOME extension"
    echo "(needed on Wayland to keep desktop icons above the wallpaper)."
fi
echo "Turn on 'Start UWP when I log in' in Shortcuts & settings to autostart it."
