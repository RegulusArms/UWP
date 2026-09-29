"""Desktop-level wallpaper windows, one per monitor.

Images are painted once into a Cairo surface (no work while idle). Videos
use a GStreamer pipeline that stays on the GPU: hardware decode ->
glupload -> gltransformation -> glimagesink, which draws straight into
the wallpaper window's X11 surface (no read-back to the CPU). Audio is never
decoded.
"""
import ctypes
import os
import warnings

import cairo
import gi
from gi.repository import Gdk, GLib, Gst, Gtk

gi.require_version('GdkX11', '3.0')
from gi.repository import GdkX11  # noqa: E402,F401  (enables get_xid)

from . import media, transform, xstack

GST_PLAY_FLAG_VIDEO = 0x1
GST_PLAY_FLAG_NATIVE_VIDEO = 0x40   # keep hw-decoded frames off the CPU


def prefer_hardware_decoders():
    """Rank hardware video decoders (NVDEC, VA-API, ...) above software
    ones; by default some tie with libvpx/libav and lose. Set
    UWP_NO_HWDEC=1 to skip."""
    if os.environ.get('UWP_NO_HWDEC'):
        return []
    bumped = []
    for f in Gst.Registry.get().get_feature_list(Gst.ElementFactory):
        klass = f.get_metadata('klass') or ''
        if (all(k in klass for k in ('Decoder', 'Video', 'Hardware'))
                and not f.get_name().startswith('vulkan')):
            f.set_rank(Gst.Rank.PRIMARY + 10)
            bumped.append(f.get_name())
    return bumped


def set_window_handle(sink, xid):
    """GstVideoOverlay.set_window_handle, via ctypes if the GstVideo
    typelib (gir1.2-gst-plugins-base-1.0) is not installed."""
    try:
        gi.require_version('GstVideo', '1.0')
        from gi.repository import GstVideo
        GstVideo.VideoOverlay.set_window_handle(sink, xid)
        return
    except (ImportError, ValueError):
        pass
    lib = ctypes.CDLL('libgstvideo-1.0.so.0')
    fn = lib.gst_video_overlay_set_window_handle
    fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    # PyGObject instances store the GObject* right after PyObject_HEAD.
    ptr = ctypes.c_void_p.from_address(id(sink) + 2 * ctypes.sizeof(
        ctypes.c_void_p)).value
    fn(ptr, xid)


def monitors():
    """Connected monitors as dicts with a stable 'key' (connector name)."""
    display = Gdk.Display.get_default()
    screen = Gdk.Screen.get_default()
    out, used = [], set()
    for i in range(display.get_n_monitors()):
        m = display.get_monitor(i)
        g = m.get_geometry()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            key = screen.get_monitor_plug_name(i)
        key = key or m.get_model() or f'Monitor-{i + 1}'
        while key in used:
            key += "'"
        used.add(key)
        out.append({
            'key': key, 'x': g.x, 'y': g.y, 'w': g.width, 'h': g.height,
            'scale': m.get_scale_factor(), 'primary': m.is_primary(),
            'model': ' '.join(filter(None, (m.get_manufacturer(),
                                            m.get_model()))),
        })
    return out


class ImageView(Gtk.DrawingArea):
    def __init__(self, wp, W, H, scale, keep=False):
        super().__init__()
        self.W, self.H, self.scale = W, H, scale
        self.surface = None
        # (path, source surface, w, h): kept only during live preview so
        # each slider tick is a repaint, not a decode from disk.
        self._src = None
        self.update(wp, keep)
        self.connect('draw', self._draw)

    def _load(self, path):
        # Cap decode size: enough for 3x zoom without keeping a gigantic
        # source image in memory.
        cap = max(self.W, self.H) * self.scale * 3
        w, h = media.image_size(path)
        pb = (media.load_image(path, cap, cap)
              if max(w, h) > cap else media.load_image(path))
        if pb.get_option('orientation') in ('5', '6', '7', '8'):
            w, h = h, w
        return pb, w or None, h or None

    def update(self, wp, keep=False):
        s = self.scale
        surf = cairo.ImageSurface(cairo.FORMAT_RGB24, self.W * s, self.H * s)
        surf.set_device_scale(s, s)
        cr = cairo.Context(surf)
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        try:
            if self._src and self._src[0] == wp['path']:
                _, src, w, h = self._src
            else:
                src, w, h = self._load(wp['path'])
                if keep:
                    src = Gdk.cairo_surface_create_from_pixbuf(src, 1, None)
            transform.cairo_paint(cr, src, wp, self.W, self.H, w, h,
                                  fast=keep)
            self._src = (wp['path'], src, w, h) if keep else None
        except GLib.Error as e:
            self._src = None
            print(f"uwp: cannot load {wp['path']}: {e.message}")
        self.surface = surf
        self.queue_draw()

    def _draw(self, _w, cr):
        cr.set_source_surface(self.surface, 0, 0)
        cr.paint()
        return True

    def pause(self, paused):
        pass

    def stop(self):
        self.surface = None


class VideoView:
    def __init__(self, wp, W, H, scale, xid):
        self.W, self.H = W, H
        self.wp = wp
        self.src = None          # (w, h) of decoded frames, from caps
        self.paused = False
        self.uri = Gst.filename_to_uri(wp['path'])

        self.pipeline = Gst.ElementFactory.make('playbin', None)
        self.pipeline.set_property('uri', self.uri)
        self.pipeline.set_property(
            'flags', GST_PLAY_FLAG_VIDEO | GST_PLAY_FLAG_NATIVE_VIDEO)
        sinkbin = Gst.parse_bin_from_description(
            'glupload ! glcolorconvert ! glvideoflip video-direction=auto ! '
            'gltransformation name=xf ortho=true ! '
            f'video/x-raw(memory:GLMemory),width={W * scale},'
            f'height={H * scale} ! glimagesink name=gsink '
            'force-aspect-ratio=false handle-events=false', True)
        set_window_handle(sinkbin.get_by_name('gsink'), xid)
        self.pipeline.set_property('video-sink', sinkbin)
        self.xf = sinkbin.get_by_name('xf')
        self.xf.get_static_pad('sink').connect('notify::caps', self._on_caps)

        # Seamless looping via segment seeks: the pipeline is never rebuilt,
        # so short clips don't churn decoders or memory.
        self._segment_started = False
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_ids = [
            bus.connect('message::async-done', self._on_async_done),
            bus.connect('message::segment-done', self._on_segment_done),
            bus.connect('message::eos', self._on_eos),
            bus.connect('message::error', self._on_error)]

    def start(self):
        self.pipeline.set_state(
            Gst.State.PAUSED if self.paused else Gst.State.PLAYING)
        return False

    def _on_caps(self, pad, _pspec):
        caps = pad.get_current_caps()
        if not caps:
            return
        s = caps.get_structure(0)
        ok_w, w = s.get_int('width')
        ok_h, h = s.get_int('height')
        if not (ok_w and ok_h):
            return
        ok, pn, pd = s.get_fraction('pixel-aspect-ratio')
        if ok and pd:
            w = w * pn / pd
        self.src = (w, h)
        self._apply()

    def _apply(self):
        if not self.src:
            return
        params = transform.gst_params(self.wp, *self.src, self.W, self.H)
        for k, v in params.items():
            self.xf.set_property(k, v)

    def update(self, wp, keep=False):
        old = transform.playback(self.wp)
        self.wp = wp
        self._apply()
        new = transform.playback(wp)
        if new == old or not self._segment_started:
            return
        if new[1:] != old[1:]:
            # Loop section changed: restart at its beginning.
            self._seek_start(Gst.SeekFlags.FLUSH)
        else:
            # Speed only: carry on from the current frame.
            ok, pos = self.pipeline.query_position(Gst.Format.TIME)
            start = pos / Gst.SECOND if ok and pos >= 0 else None
            if start is not None and new[2] and start >= new[2]:
                start = None
            self._seek_start(Gst.SeekFlags.FLUSH, start)

    def _seek_start(self, flags, position=None):
        """Segment seek over the loop section at the chosen speed; the
        segment-done at its end loops it without rebuilding anything."""
        speed, start, end = transform.playback(self.wp)
        pos = start if position is None else position
        self.pipeline.seek(
            speed, Gst.Format.TIME,
            flags | Gst.SeekFlags.SEGMENT | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET, int(pos * Gst.SECOND),
            Gst.SeekType.SET if end else Gst.SeekType.NONE,
            int(end * Gst.SECOND) if end else -1)

    def _on_async_done(self, _bus, _msg):
        if not self._segment_started:
            self._segment_started = True
            self._seek_start(Gst.SeekFlags.FLUSH)

    def _on_segment_done(self, _bus, _msg):
        self._seek_start(Gst.SeekFlags.NONE)

    def _on_eos(self, _bus, _msg):
        # Only reached if segment seeking isn't supported by the demuxer.
        self._seek_start(Gst.SeekFlags.FLUSH)

    def _on_error(self, _bus, msg):
        err, dbg = msg.parse_error()
        print(f"uwp: video error ({self.wp['path']}): {err.message}\n{dbg}")

    def pause(self, paused):
        if paused != self.paused:
            self.paused = paused
            self.pipeline.set_state(
                Gst.State.PAUSED if paused else Gst.State.PLAYING)

    def stop(self):
        bus = self.pipeline.get_bus()
        for i in self._bus_ids:
            bus.disconnect(i)
        bus.remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)


class MonitorWindow(Gtk.Window):
    def __init__(self, mon):
        super().__init__(title=f"UWP wallpaper {mon['key']}")
        self.mon = mon
        self.view = None
        self.path = None
        self.set_type_hint(Gdk.WindowTypeHint.DESKTOP)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        # No set_keep_below(): mutter puts "below" windows in a layer ABOVE
        # true desktop windows, which would hide the desktop icons.
        self.set_accept_focus(False)
        self.stick()
        self.set_app_paintable(True)
        self.connect('draw', self._draw_black)
        self.move(mon['x'], mon['y'])
        self.set_default_size(mon['w'], mon['h'])
        self.connect('realize', self._on_realize)

    def _on_realize(self, _w):
        gw = self.get_window()
        gw.move_resize(self.mon['x'], self.mon['y'],
                       self.mon['w'], self.mon['h'])

    @staticmethod
    def _draw_black(_w, cr):
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        return False

    def set_wallpaper(self, wp, keep=False):
        m = self.mon
        if self.view and self.path == wp['path']:
            self.view.update(wp, keep)
            return
        self.clear()
        self.path = wp['path']
        if media.kind(wp['path']) == 'video':
            self.show_all()
            xid = self.get_window().get_xid()
            self.view = VideoView(wp, m['w'], m['h'], m['scale'], xid)
            GLib.idle_add(self.view.start)
        else:
            self.view = ImageView(wp, m['w'], m['h'], m['scale'], keep)
            self.add(self.view)
            self.show_all()

    def clear(self):
        if self.view:
            self.view.stop()
            child = self.get_child()
            if child:
                self.remove(child)
            self.view = None
            self.path = None

    def pause(self, paused):
        if self.view:
            self.view.pause(paused)

    def is_video(self):
        return isinstance(self.view, VideoView)


class Desktop:
    """Owns the per-monitor windows and applies profiles to them."""

    def __init__(self):
        self.windows = {}        # monitor key -> MonitorWindow
        self.profile = None
        self.user_paused = False
        self.covered = set()
        self.previewing = False
        self.stack = StackKeeper(self)

    def apply(self, profile, preview=False):
        """Show profile. preview=True is the GUI's live preview: images keep
        their decoded source for fast repaints and videos never pause."""
        self.profile = profile
        self.previewing = preview
        mons = {m['key']: m for m in monitors()}
        for key, win in list(self.windows.items()):
            m = mons.get(key)
            geom = lambda d: (d['x'], d['y'], d['w'], d['h'], d['scale'])
            if m is None or geom(m) != geom(win.mon):
                win.clear()
                win.destroy()
                del self.windows[key]
        for key, m in mons.items():
            wp = profile['monitors'].get(key)
            if wp and os.path.exists(wp['path']):
                win = self.windows.get(key)
                if win is None:
                    win = self.windows[key] = MonitorWindow(m)
                    win.connect('map-event',
                                lambda *_: self.stack.schedule() and False)
                win.set_wallpaper(wp, keep=preview)
            elif key in self.windows:
                win = self.windows.pop(key)
                win.clear()
                win.destroy()
        self._update_pause()
        self.stack.schedule()

    def reapply(self):
        if self.profile:
            self.apply(self.profile)

    def set_user_paused(self, paused):
        self.user_paused = paused
        self._update_pause()

    def set_covered(self, keys):
        self.covered = set(keys)
        self._update_pause()

    def _update_pause(self):
        for key, win in self.windows.items():
            win.pause(not self.previewing and
                      (self.user_paused or key in self.covered))

    def has_video(self):
        return any(w.is_video() for w in self.windows.values())

    def shutdown(self):
        for win in self.windows.values():
            win.clear()
            win.destroy()
        self.windows.clear()


_wnck = None


def wnck_screen():
    """(Wnck module, default screen), or (None, None) if unavailable."""
    global _wnck
    if _wnck is None:
        try:
            gi.require_version('Wnck', '3.0')
            from gi.repository import Wnck
            screen = Wnck.Screen.get_default()
            screen.force_update()
            _wnck = (Wnck, screen)
        except (ImportError, ValueError, AttributeError):
            _wnck = (None, None)
    return _wnck


class StackKeeper:
    """Keeps the wallpaper windows underneath every other desktop-type
    window, so the desktop-icons extension (DING) draws its icons on top and
    receives the clicks. DING lowers its own transparent windows whenever
    they are raised, which would otherwise slip them under ours."""

    def __init__(self, desktop):
        self.desktop = desktop
        self._pending = 0
        self.Wnck, self.screen = wnck_screen()
        if self.screen is not None:
            self.screen.connect('window-stacking-changed',
                                lambda _s: self.schedule())

    def schedule(self):
        if not self._pending:
            self._pending = GLib.timeout_add(50, self._check)
        return True

    def _ours(self):
        return {win.get_window().get_xid()
                for win in self.desktop.windows.values()
                if win.get_window() is not None and win.get_mapped()}

    def _check(self):
        # Mutter ignores plain XLowerWindow from our never-focused windows,
        # so restack with the EWMH pager message instead.
        self._pending = 0
        ours = self._ours()
        if not ours:
            return False
        if self.screen is None:
            xstack.lower(ours)
            return False
        # Bottom-to-top; any foreign desktop window under one of ours means
        # we must drop ours to the bottom again.
        foreign_below = False
        for w in self.screen.get_windows_stacked():
            xid = w.get_xid()
            if xid in ours:
                if foreign_below:
                    xstack.lower(ours)
                    break
            elif w.get_window_type() == self.Wnck.WindowType.DESKTOP:
                foreign_below = True
        return False


class CoverWatcher:
    """Reports which monitors are hidden behind a fullscreen/maximized (or
    near-full-size) window on the current workspace, so their videos can be
    paused. X11 only; silently disabled if libwnck is unavailable."""

    def __init__(self, callback):
        self.callback = callback
        self.enabled = False
        self.last = None
        self.Wnck, self.screen = wnck_screen()
        if self.screen is not None:
            GLib.timeout_add(1000, self._tick)

    def set_enabled(self, enabled):
        self.enabled = enabled and self.screen is not None
        self.last = None
        if not self.enabled:
            self.callback(set())

    def _tick(self):
        if self.enabled:
            covered = self._compute()
            if covered != self.last:
                self.last = covered
                self.callback(covered)
        return True

    def _compute(self):
        Wnck = self.Wnck
        ws = self.screen.get_active_workspace()
        wins = []
        for w in self.screen.get_windows():
            if (w.get_window_type() != Wnck.WindowType.NORMAL
                    or w.is_minimized()
                    or (ws and not w.is_visible_on_workspace(ws))):
                continue
            wins.append((w.is_fullscreen() or w.is_maximized(),
                         w.get_geometry()))
        covered = set()
        for m in monitors():
            area = m['w'] * m['h']
            for full, (x, y, w, h) in wins:
                ix = max(0, min(x + w, m['x'] + m['w']) - max(x, m['x']))
                iy = max(0, min(y + h, m['y'] + m['h']) - max(y, m['y']))
                if ix * iy >= (0.5 if full else 0.9) * area:
                    covered.add(m['key'])
                    break
        return covered
