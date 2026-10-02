import os
import sys

# Desktop-type windows need X11; on Wayland this runs through XWayland, so
# the process behaves exactly as in an Xorg session. Forced, not defaults:
# Wayland sessions (Ubuntu 26.04) export GDK_BACKEND=wayland, and GStreamer
# GL / EGL pick Wayland whenever WAYLAND_DISPLAY is set, then abort on our
# X11 window handle. Apps we launch get it back (see child_env()).
if os.environ.get('WAYLAND_DISPLAY'):
    os.environ['UWP_WAYLAND_DISPLAY'] = os.environ.pop('WAYLAND_DISPLAY')
os.environ['GDK_BACKEND'] = 'x11'
os.environ['GST_GL_WINDOW'] = 'x11'
# The NVIDIA driver busy-waits on vsync'd buffer swaps, costing ~15% CPU
# per video. Under a compositor (GNOME always composites these windows) the
# compositor does the vsync, and GStreamer paces frames to the video clock,
# so app-side vsync only burns CPU. Affects this process only.
os.environ.setdefault('__GL_SYNC_TO_VBLANK', '0')
os.environ.setdefault('vblank_mode', '0')   # same for Mesa drivers

import gi  # noqa: E402

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Gst', '1.0')
gi.require_version('GdkPixbuf', '2.0')


def main():
    if not os.environ.get('DISPLAY'):
        sys.exit('uwp: no X display (DISPLAY is not set). UWP needs an Xorg '
                 'session, or XWayland under Wayland.')
    from .app import App
    return App().run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
