"""Ask the window manager to restack a window, via _NET_RESTACK_WINDOW.

Mutter ignores plain XLowerWindow requests from windows that never had
user interaction (ours never take focus), but honours the EWMH pager
message, which is what we need to push wallpaper windows underneath the
desktop-icons windows.
"""
import ctypes
import ctypes.util

_BELOW = 1
_CLIENT_MESSAGE = 33
_SUBSTRUCTURE_MASKS = (1 << 19) | (1 << 20)  # Notify | Redirect


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int),
                ('serial', ctypes.c_ulong),
                ('send_event', ctypes.c_int),
                ('display', ctypes.c_void_p),
                ('window', ctypes.c_ulong),
                ('message_type', ctypes.c_ulong),
                ('format', ctypes.c_int),
                ('data', ctypes.c_long * 5)]


class _XEvent(ctypes.Union):
    _fields_ = [('xclient', _XClientMessageEvent),
                ('pad', ctypes.c_long * 24)]


_x = None


def _xlib():
    global _x
    if _x is None:
        lib = ctypes.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6')
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XDefaultRootWindow.restype = ctypes.c_ulong
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XInternAtom.restype = ctypes.c_ulong
        lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                    ctypes.c_int]
        lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                   ctypes.c_int, ctypes.c_long,
                                   ctypes.POINTER(_XEvent)]
        lib.XFlush.argtypes = [ctypes.c_void_p]
        dpy = lib.XOpenDisplay(None)
        if not dpy:
            _x = (None, None, None, None)
        else:
            _x = (lib, dpy, lib.XDefaultRootWindow(dpy),
                  lib.XInternAtom(dpy, b'_NET_RESTACK_WINDOW', 0))
    return _x


def lower(xids, sibling=0):
    """Move each window to the bottom of its stacking layer, or directly
    below `sibling` if given. Returns False if X is unavailable."""
    lib, dpy, root, atom = _xlib()
    if not lib:
        return False
    for xid in xids:
        ev = _XEvent()
        cm = ev.xclient
        cm.type = _CLIENT_MESSAGE
        cm.send_event = 1
        cm.window = xid
        cm.message_type = atom
        cm.format = 32
        cm.data[0] = 2          # source indication: pager
        cm.data[1] = sibling
        cm.data[2] = _BELOW
        lib.XSendEvent(dpy, root, 0, _SUBSTRUCTURE_MASKS, ctypes.byref(ev))
    lib.XFlush(dpy)
    return True
