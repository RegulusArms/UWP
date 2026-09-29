#!/bin/sh
# Reinstalls UWP for the current user: stops it, removes the installed
# pieces, installs again and restarts it. Settings (library, profiles,
# shortcuts) and "Start UWP when I log in" are kept.
#
#   ./reinstall.sh           keep settings
#   ./reinstall.sh --clean   start fresh: also delete settings and cache
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
case "$1" in
    "") "$HERE/uninstall.sh" --keep-autostart ;;
    --clean) "$HERE/uninstall.sh" --purge ;;
    -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac
echo
"$HERE/install.sh"
