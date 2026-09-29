"""Per-monitor slideshow of photos and videos with transitions.

Three desktop windows per monitor, top to bottom: the incoming layer, the
current layer, and a black backdrop (so a fade never reveals GNOME's own
background). Each layer is an ordinary MonitorWindow, so items render
exactly like single wallpapers. Transitions are done by the compositor on
the GPU: fades animate the incoming window's opacity, wipes animate its
clip region.
"""
import json
import os
import random

from gi.repository import GLib

from . import media, transform
from .renderer import MonitorWindow, VideoView

PRELOAD = 1.5          # seconds before a transition to load the next item
FRAME_MS = 16

_durations = {}        # path -> seconds, shared by all players


def video_length(path):
    if path not in _durations:
        _durations[path] = media.video_duration(path) or 0.0
    return _durations[path]


def show_time(show, item):
    """Seconds this item stays up, or None if unknown (video length could
    not be read; its end-of-play event is used instead)."""
    if media.kind(item['path']) != 'video':
        return transform.photo_duration(show, item)
    speed, start, end = transform.playback(item)
    length = video_length(item['path'])
    stop = end or length
    if stop <= start:
        return None
    return transform.video_plays(item) * (stop - start) / speed


class _Timer:
    """One-shot timer that can be paused and resumed."""

    def __init__(self, seconds, callback):
        self.remaining = max(0.0, seconds)
        self.callback = callback
        self.id = 0
        self.t0 = 0
        self.resume()

    def resume(self):
        if self.id or self.remaining is None:
            return
        self.t0 = GLib.get_monotonic_time()
        self.id = GLib.timeout_add(int(self.remaining * 1000), self._fire)

    def pause(self):
        if self.id:
            GLib.source_remove(self.id)
            self.id = 0
            elapsed = (GLib.get_monotonic_time() - self.t0) / 1e6
            self.remaining = max(0.0, self.remaining - elapsed)

    def cancel(self):
        if self.id:
            GLib.source_remove(self.id)
        self.id = 0
        self.remaining = None

    def _fire(self):
        self.id = 0
        self.remaining = None
        self.callback()
        return False


class SlideshowPlayer:
    kind = 'slideshow'

    def __init__(self, mon, desktop):
        self.mon = mon
        self.desktop = desktop
        self.backdrop = MonitorWindow(mon)       # plain black window
        desktop.watch_map(self.backdrop)
        self.backdrop.show_all()
        self.layers = [self._new_layer(), self._new_layer()]
        self.cur = 0                  # index of the layer on screen
        self.show = None              # slideshow settings (entry dict)
        self.items = []
        self.order = []
        self.pos = 0                  # position in self.order
        self.sig = None               # what is currently running
        self.static = False           # holding one item (GUI preview)
        self.timers = []
        self.anim = 0
        self.prepared = None          # item index loaded in the other layer
        self.transition = None
        self.played = 0.0             # seconds the current item already ran
        self.paused = False
        self.gen = 0                  # invalidates stale callbacks

    def _new_layer(self):
        win = MonitorWindow(self.mon)
        self.desktop.watch_map(win)
        return win

    # ---- output interface ---------------------------------------------
    def show_wp(self, entry, keep=False):
        items = [i for i in entry.get('items', [])
                 if os.path.exists(i.get('path', ''))]
        if not items:
            return
        preview_item = entry.get('_preview_item') if keep else None
        if preview_item is not None and 0 <= preview_item < len(entry['items']):
            item = entry['items'][preview_item]
            if os.path.exists(item.get('path', '')):
                self._hold(item, keep)
                return
        sig = json.dumps({k: v for k, v in entry.items()
                          if k != '_preview_item'}, sort_keys=True)
        if sig != self.sig or self.static:
            self.sig = sig
            self._start(entry, items)

    def stack_windows(self):
        incoming = self.layers[1 - self.cur]
        return [incoming, self.layers[self.cur], self.backdrop]

    def pause(self, paused):
        self.paused = paused
        for t in self.timers:
            t.pause() if paused else t.resume()
        self.layers[self.cur].pause(paused)
        if self.anim:
            self.layers[1 - self.cur].pause(paused)

    def is_video(self):
        return any(layer.is_video() for layer in self.layers)

    def destroy_output(self):
        self._stop()
        for win in self.layers + [self.backdrop]:
            win.destroy_output()

    # ---- running the show ---------------------------------------------
    def _stop(self):
        self.gen += 1
        for t in self.timers:
            t.cancel()
        self.timers = []
        if self.anim:
            GLib.source_remove(self.anim)
            self.anim = 0
        self.prepared = None

    def _reset_layer(self, layer):
        layer.clear()
        layer.set_clip(None, 0, 0, 0)
        layer.set_opacity(1.0)

    def _hold(self, item, keep):
        """GUI live preview of one item: show it, no timers."""
        self._stop()
        self.static = True
        self.sig = None
        other = self.layers[1 - self.cur]
        if other.get_mapped():
            self._reset_layer(other)
            other.hide()
        layer = self.layers[self.cur]
        layer.set_clip(None, 0, 0, 0)
        layer.set_opacity(1.0)
        layer.set_wallpaper(item, keep)       # updates in place if same file
        if isinstance(layer.view, VideoView):
            layer.view.max_plays = 0          # loop while adjusting
            layer.view.on_finished = None
        self.desktop.stack.schedule()

    def _start(self, entry, items):
        self._stop()
        self.static = False
        self.show = entry
        self.items = items
        self.order = list(range(len(items)))
        if entry.get('shuffle'):
            random.shuffle(self.order)
        self.pos = 0
        self.played = 0.0
        for layer in self.layers:
            self._reset_layer(layer)
        self.layers[1 - self.cur].hide()
        self._load(self.layers[self.cur], self._item(), playing=True)
        self._schedule()

    def _item(self, pos=None):
        return self.items[self.order[self.pos if pos is None else pos]]

    def _load(self, layer, item, playing):
        layer.clear()
        layer.set_wallpaper(item)
        view = layer.view
        if isinstance(view, VideoView):
            view.paused = self.paused or not playing
            if len(self.items) > 1:
                gen = self.gen
                view.max_plays = transform.video_plays(item)
                view.on_finished = lambda: self._video_finished(gen, layer)

    def _schedule(self):
        if len(self.items) < 2:
            return                      # a single item just stays up
        item = self._item()
        t = show_time(self.show, item)
        tt = self._transition_time()
        if t is None:
            return                      # wait for the video's end event
        if media.kind(item['path']) == 'video':
            # It started playing when its transition began; fade out during
            # its last seconds.
            begin = max(0.0, t - tt - self.played)
        else:
            begin = t
        self.timers = [_Timer(max(0.0, begin - PRELOAD), self._prepare),
                       _Timer(begin, self._begin)]
        if self.paused:
            for timer in self.timers:
                timer.pause()

    def _transition_time(self):
        return max(0.0, float(self.show.get('transition_time') or 0.0))

    def _video_finished(self, gen, layer):
        # Safety net when the video's length was unknown or timers drifted.
        if gen != self.gen or layer is not self.layers[self.cur] \
                or self.anim:
            return
        for t in self.timers:
            t.cancel()
        self.timers = []
        self._begin()

    def _next_pos(self):
        nxt = self.pos + 1
        if nxt >= len(self.order):
            if self.show.get('shuffle') and len(self.order) > 2:
                last = self.order[-1]
                random.shuffle(self.order)
                if self.order[0] == last:   # avoid an immediate repeat
                    self.order.append(self.order.pop(0))
            nxt = 0
        return nxt

    def _prepare(self):
        if self.prepared is not None or self.static:
            return
        nxt = self._next_pos()
        kind = self.show.get('transition') or 'fade'
        if kind == 'random':
            kind = random.choice([k for k in transform.TRANSITIONS
                                  if k not in ('random', 'none')])
        if self._transition_time() <= 0:
            kind = 'none'
        self.transition = kind
        layer = self.layers[1 - self.cur]
        self._reset_layer(layer)
        layer.realize()
        if kind.startswith('wipe'):
            layer.set_clip(0, 0, 0, 0)          # fully clipped, opaque
        else:
            layer.set_opacity(0.0)
        self._load(layer, self._item(nxt), playing=False)
        self.prepared = nxt
        self.desktop.stack.schedule()

    def _begin(self):
        if self.static or self.anim:
            return
        if self.prepared is None:
            self._prepare()
        inc, out = self.layers[1 - self.cur], self.layers[self.cur]
        if isinstance(inc.view, VideoView):
            inc.view.pause(self.paused)
        duration = self._transition_time()
        if self.transition == 'none' or duration <= 0:
            self.played = 0.0
            self._finish()
            return
        self.played = duration
        t0 = GLib.get_monotonic_time()
        W, H = self.mon['w'], self.mon['h']
        kind = self.transition

        def step():
            p = min(1.0, (GLib.get_monotonic_time() - t0) / (duration * 1e6))
            e = p * p * (3 - 2 * p)            # smoothstep
            if kind == 'fade':
                inc.set_opacity(e)
            elif kind == 'black':
                if p < 0.5:
                    out.set_opacity(1 - 2 * p)
                else:
                    out.set_opacity(0.0)
                    inc.set_opacity(2 * p - 1)
            elif kind == 'wipe-left':
                inc.set_clip(W * (1 - e), 0, W * e + 1, H)
            elif kind == 'wipe-right':
                inc.set_clip(0, 0, W * e, H)
            elif kind == 'wipe-up':
                inc.set_clip(0, H * (1 - e), W, H * e + 1)
            elif kind == 'wipe-down':
                inc.set_clip(0, 0, W, H * e)
            if p >= 1.0:
                self.anim = 0
                self._finish()
                return False
            return True
        self.anim = GLib.timeout_add(FRAME_MS, step)

    def _finish(self):
        inc, out = self.layers[1 - self.cur], self.layers[self.cur]
        inc.set_opacity(1.0)
        inc.set_clip(None, 0, 0, 0)
        if isinstance(inc.view, VideoView):
            inc.view.pause(self.paused)
        self._reset_layer(out)
        out.hide()
        self.cur = 1 - self.cur
        self.pos = self.prepared if self.prepared is not None \
            else self._next_pos()
        self.prepared = None
        self.gen += 1
        # Re-arm the finished callback of the new current video with the
        # new generation.
        view = inc.view
        if isinstance(view, VideoView) and len(self.items) > 1:
            gen = self.gen
            view.on_finished = lambda: self._video_finished(gen, inc)
        self.desktop.stack.schedule()
        self._schedule()
