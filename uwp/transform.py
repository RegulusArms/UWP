"""Wallpaper placement math shared by the desktop renderer and the GUI preview.

A wallpaper entry is a dict:
    path      absolute file path
    mode      'fill' | 'fit' | 'stretch' | 'center'
    zoom      multiplier on top of the mode (1.0 = 100%)
    offset_x  shift as a fraction of the monitor width  (+ = right)
    offset_y  shift as a fraction of the monitor height (+ = down)
    rotation  degrees, clockwise
    speed, loop_start, loop_end   video playback (see playback())

The same numbers drive Cairo (images, previews) and GStreamer's
gltransformation (videos), so the preview matches the desktop exactly.
"""
import math

MODES = ('fill', 'fit', 'stretch', 'center')
MODE_LABELS = {
    'fill': 'Fill (crop to cover)',
    'fit': 'Fit (letterbox)',
    'stretch': 'Stretch',
    'center': 'Center (native size)',
}


TRANSFORM_KEYS = ('mode', 'zoom', 'offset_x', 'offset_y', 'rotation')


def new_wallpaper(path):
    return {'path': path, 'mode': 'fill', 'zoom': 1.0,
            'offset_x': 0.0, 'offset_y': 0.0, 'rotation': 0.0,
            # videos only: playback rate and loop section in seconds
            # (loop_end 0 = until the end of the video)
            'speed': 1.0, 'loop_start': 0.0, 'loop_end': 0.0}


def playback(wp):
    """Normalized (speed, loop_start, loop_end) for a wallpaper entry;
    loop_end 0 means the natural end of the video."""
    speed = min(4.0, max(0.1, float(wp.get('speed') or 1.0)))
    start = max(0.0, float(wp.get('loop_start') or 0.0))
    end = float(wp.get('loop_end') or 0.0)
    if end and end <= start + 0.05:
        end = 0.0
    return speed, start, end


def base_size(mode, sw, sh, W, H, px_scale=1.0):
    """Size in canvas px of a sw x sh source placed on a W x H canvas.

    px_scale is canvas px per real monitor px (1.0 on the desktop, smaller in
    the preview); only 'center' needs it since it works in native pixels.
    """
    if sw <= 0 or sh <= 0:
        return W, H
    if mode == 'stretch':
        return W, H
    if mode == 'center':
        return sw * px_scale, sh * px_scale
    k = max(W / sw, H / sh) if mode == 'fill' else min(W / sw, H / sh)
    return sw * k, sh * k


def placement(wp, sw, sh, W, H, px_scale=1.0):
    """Return (center_x, center_y, width, height, rotation_deg) in canvas px."""
    bw, bh = base_size(wp.get('mode', 'fill'), sw, sh, W, H, px_scale)
    z = wp.get('zoom', 1.0)
    return (W / 2 + wp.get('offset_x', 0.0) * W,
            H / 2 + wp.get('offset_y', 0.0) * H,
            bw * z, bh * z, wp.get('rotation', 0.0))


def cairo_paint(cr, src, wp, W, H, src_w=None, src_h=None, px_scale=1.0,
                fast=False):
    """Paint src (a GdkPixbuf or cairo.ImageSurface) onto a W x H Cairo
    canvas according to wp.

    src_w/src_h are the real media dimensions (src may be a smaller
    thumbnail of it); they default to the src size. fast=True trades
    downscale quality for speed (used while dragging in live preview).
    """
    from gi.repository import Gdk
    import cairo
    pw, ph = src.get_width(), src.get_height()
    sw, sh = src_w or pw, src_h or ph
    cx, cy, w, h, rot = placement(wp, sw, sh, W, H, px_scale)
    cr.save()
    cr.translate(cx, cy)
    cr.rotate(math.radians(rot))
    cr.scale(w / pw, h / ph)
    if isinstance(src, cairo.Surface):
        cr.set_source_surface(src, -pw / 2, -ph / 2)
    else:
        Gdk.cairo_set_source_pixbuf(cr, src, -pw / 2, -ph / 2)
    cr.get_source().set_filter(cairo.FILTER_BILINEAR if fast
                               else cairo.FILTER_GOOD)
    cr.paint()
    cr.restore()


def gst_params(wp, sw, sh, W, H):
    """Properties for gltransformation (ortho) rendering into a W x H output.

    Verified empirically: scale-x/y are fractions of the output size,
    translation-x/y are fractions of output width/height with +y down, and
    rotation-z is clockwise degrees applied after scaling (aspect preserved).
    """
    _, _, w, h, rot = placement(wp, sw, sh, W, H)
    return {
        'scale-x': w / W,
        'scale-y': h / H,
        'translation-x': wp.get('offset_x', 0.0),
        'translation-y': wp.get('offset_y', 0.0),
        'rotation-z': rot,
    }
