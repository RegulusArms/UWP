#!/bin/sh
# Removes UWP for the current user.
#
#   ./uninstall.sh           remove UWP, keep settings (library, profiles)
#   ./uninstall.sh --purge   also delete settings and the thumbnail cache
#
# System packages (GTK, GStreamer, ffmpeg, ...) are left alone: other
# programs use them too.
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.RegulusArms.UWP
PURGE=0
KEEP_AUTOSTART=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --keep-autostart) KEEP_AUTOSTART=1 ;;   # used by reinstall.sh
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}"

# 1. Stop the running instance (its wallpaper windows close and GNOME's
#    normal background shows again).
if pgrep -f '^/usr/bin/python3 -m uwp' >/dev/null; then
    echo "Stopping UWP..."
    "$HERE/bin/uwp" --quit >/dev/null 2>&1
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        pgrep -f '^/usr/bin/python3 -m uwp' >/dev/null || break
        sleep 0.3
    done
    pkill -f '^/usr/bin/python3 -m uwp' 2>/dev/null
fi

# 2. Remove UWP's GNOME custom shortcuts, leaving any others untouched.
#    (dconf talks to the settings daemon directly, so this also works from
#    terminals whose environment is polluted by snaps.)
KB=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
if command -v dconf >/dev/null; then
    LIST="$(dconf read $KB)"
    NEW="$(printf '%s' "$LIST" | /usr/bin/python3 -c '
import ast, sys
raw = sys.stdin.read().strip()
if raw.startswith("@as"):
    raw = raw[3:].strip()
paths = ast.literal_eval(raw) if raw else []
keep = [p for p in paths if "/custom-keybindings/uwp-" not in p]
if keep != paths:
    print(repr(keep) if keep else "@as []")
')"
    if [ -n "$NEW" ]; then
        dconf write $KB "$NEW"
        echo "Removed UWP keyboard shortcuts."
    fi
    for dir in $(dconf list $KB/ 2>/dev/null | grep '^uwp-'); do
        dconf reset -f "$KB/$dir"
    done
fi

# 3. Launcher, app-grid entry, icon, autostart.
rm -f "$HOME/.local/share/applications/$APP_ID.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg"
if [ -L "$HOME/.local/bin/uwp" ]; then
    rm -f "$HOME/.local/bin/uwp"
fi
if [ "$KEEP_AUTOSTART" = 0 ]; then
    rm -f "$CONFIG/autostart/$APP_ID.desktop"
fi
command -v update-desktop-database >/dev/null && \
    update-desktop-database -q "$HOME/.local/share/applications" 2>/dev/null

# 4. Settings and cache.
if [ "$PURGE" = 1 ]; then
    rm -rf "$CONFIG/uwp" "$CACHE/uwp"
    echo "Deleted settings ($CONFIG/uwp) and cache ($CACHE/uwp)."
else
    [ -d "$CONFIG/uwp" ] && \
        echo "Kept your settings in $CONFIG/uwp (use --purge to delete them)."
fi

echo "UWP uninstalled. The program files in $HERE were not touched."
