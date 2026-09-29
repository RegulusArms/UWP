import os
import sys

# Desktop-type windows need X11; on Wayland this runs through XWayland.
os.environ.setdefault('GDK_BACKEND', 'x11')
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
    from .app import App
    return App().run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
