"""Per-monitor slideshow of photos and videos with transitions.

Three desktop windows per monitor, top to bottom: the incoming layer, the
current layer, and a black backdrop (so a fade never reveals GNOME's own
background). Each layer is an ordinary MonitorWindow, so items render
exactly like single wallpapers. Transitions are done by the compositor on
the GPU: fades animate the incoming window's opacity, wipes animate its
clip region.

All players run on one Clock, so monitors with the same timings change at
the same moment and their transitions step on the same frames. Photos for
the next slide are decoded in worker threads; the main loop only swaps in
the finished picture, so no monitor's transition stalls while another
monitor loads.
"""
import json
import math
import os
import random
import threading

from gi.repository import GLib

from . import media, transform
from .renderer import MonitorWindow, VideoView, log, render_image

PRELOAD = 3.0          # seconds before a transition to load the next item
FRAME_MS = 16
LATE = 0.1             # a transition starting later than this skips ahead

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


class Clock:
    """The timeline every slideshow shares. Events due at the same moment
    fire in one main-loop pass, and all running transitions are stepped
    from one frame timer with the same timestamp."""

    def __init__(self):
        self.epoch = self.now()
        self._events = []        # [when, seq, callback or None]
        self._seq = 0
        self._timer = 0
        self._steps = []
        self._anim = 0

    @staticmethod
    def now():
        return GLib.get_monotonic_time() / 1e6

    def boundary(self, period, after):
        """The first multiple of period, counted from the epoch, later than
        after. Shows with equal periods thus change together however far
        apart they started."""
        n = math.floor((after - self.epoch) / period) + 1
        return self.epoch + n * period

    def at(self, when, callback):
        self._seq += 1
        ev = [when, self._seq, callback]
        self._events.append(ev)
        self._arm()
        return ev

    def cancel(self, ev):
        if ev is None:
            return
        ev[2] = None             # may already be in a batch being fired
        if ev in self._events:
            self._events.remove(ev)
            self._arm()

    def _arm(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._events:
            when = min(e[0] for e in self._events)
            ms = max(0, math.ceil((when - self.now()) * 1000))
            self._timer = GLib.timeout_add(ms, self._fire)

    def _fire(self):
        self._timer = 0
        now = self.now()
        due = sorted(e for e in self._events if e[0] <= now + 0.002)
        for e in due:
            self._events.remove(e)
        for e in due:
            if e[2] is not None:
                e[2]()
        self._arm()
        return False

    def animate(self, step):
        """Call step(now) every frame until it returns False."""
        self._steps.append(step)
        if not self._anim:
            self._anim = GLib.timeout_add(FRAME_MS, self._frame)

    def stop(self, step):
        if step in self._steps:
            self._steps.remove(step)

    def _frame(self):
        now = self.now()
        for step in list(self._steps):
            if step in self._steps and not step(now):
                self._steps.remove(step)
        if not self._steps:
            self._anim = 0
            return False
        return True


class SlideshowPlayer:
    kind = 'slideshow'

    def __init__(self, mon, desktop):
        self.mon = mon
        self.desktop = desktop
        self.clock = desktop.clock
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
        # Timeline: the next transition starts at next_at (clock time), or,
        # while frozen, `left` seconds after it thaws. It is frozen while
        # the user paused, or while the item whose time is running is a
        # video and videos are paused (e.g. the monitor is covered).
        self.next_at = None
        self.left = None
        self.timeline_video = False
        self.ev_prepare = None
        self.ev_begin = None
        self.anim = None              # running transition step
        self.prepared = None          # item index loaded in the other layer
        self.loading = False          # a worker is rendering the next photo
        self.want_prepare = False     # preload came due during a transition
        self.begin_at = None          # transition waiting for the preload
        self.transition = None
        self.hold_videos = False
        self.hold_all = False
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

    def pause(self, paused, videos_only=False):
        """videos_only: pause videos but keep the photo timeline running
        (monitor covered by a window), rather than freeze the whole show
        (the user's pause)."""
        self.hold_videos = paused
        self.hold_all = paused and not videos_only
        self.layers[self.cur].pause(paused)
        if self.anim:
            self.layers[1 - self.cur].pause(paused)
        self._update_frozen()

    def is_video(self):
        return any(layer.is_video() for layer in self.layers)

    def destroy_output(self):
        self._stop()
        for win in self.layers + [self.backdrop]:
            win.destroy_output()

    # ---- timeline -----------------------------------------------------
    def _frozen(self):
        return self.hold_all or (self.hold_videos and self.timeline_video)

    def _update_frozen(self):
        if self._frozen():
            if self.next_at is not None:
                self.left = max(0.0, self.next_at - self.clock.now())
                self.next_at = None
                self._cancel_events()
        elif self.left is not None:
            self.next_at = self.clock.now() + self.left
            self.left = None
            self._arm()

    def _set_timeline(self, begin_at, video):
        """Next transition at clock time begin_at (None: when the current
        video ends), timed by a video's playback if video."""
        self._cancel_events()
        self.timeline_video = video
        self.next_at, self.left = begin_at, None
        if begin_at is None:
            return
        self._update_frozen()
        if self.next_at is not None:
            self._arm()

    def _arm(self):
        self._cancel_events()
        if len(self.items) < 2 or self.next_at is None:
            return
        self.ev_prepare = self.clock.at(self.next_at - PRELOAD, self._prepare)
        self.ev_begin = self.clock.at(self.next_at, self._begin)

    def _cancel_events(self):
        self.clock.cancel(self.ev_prepare)
        self.clock.cancel(self.ev_begin)
        self.ev_prepare = self.ev_begin = None

    def _length(self, item):
        """Seconds from the start of item's transition in to the start of
        the next one, or None if unknown (wait for the video's end)."""
        t = show_time(self.show, item)
        tt = self._transition_time()
        if t is None:
            return None
        if media.kind(item['path']) == 'video':
            return max(tt, t - tt)    # fade out during its last seconds
        return max(tt, t)

    # ---- running the show ---------------------------------------------
    def _stop(self):
        self.gen += 1
        self._cancel_events()
        self.next_at = self.left = None
        if self.anim:
            self.clock.stop(self.anim)
            self.anim = None
        self.prepared = None
        self.loading = False
        self.want_prepare = False
        self.begin_at = None

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
        for layer in self.layers:
            self._reset_layer(layer)
        self.layers[1 - self.cur].hide()
        item = self._item()
        self._load(self.layers[self.cur], item, playing=True)
        length = self._length(item)
        now = self.clock.now()
        if length is None:
            begin = None
        elif media.kind(item['path']) == 'video':
            begin = now + length
        else:
            # Snap to the shared grid, leaving time to preload.
            begin = self.clock.boundary(length, now + min(length / 2,
                                                          PRELOAD + 0.5))
        self._set_timeline(begin, media.kind(item['path']) == 'video')

    def _item(self, pos=None):
        return self.items[self.order[self.pos if pos is None else pos]]

    def _load(self, layer, item, playing, surface=None):
        layer.clear()
        layer.set_wallpaper(item, surface=surface)
        view = layer.view
        if isinstance(view, VideoView):
            view.paused = self.hold_videos or not playing
            if len(self.items) > 1:
                gen = self.gen
                view.max_plays = transform.video_plays(item)
                view.on_finished = lambda: self._video_finished(gen, layer)

    def _transition_time(self):
        return max(0.0, float(self.show.get('transition_time') or 0.0))

    def _video_finished(self, gen, layer):
        # Safety net when the video's length was unknown or timers drifted.
        if gen != self.gen or layer is not self.layers[self.cur] \
                or self.anim:
            return
        self._cancel_events()
        self._begin(self.clock.now())

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
        """Load the next item into the hidden layer. Photos render in a
        worker thread; _begin waits for them if they are late."""
        if self.static or self.loading:
            return
        if self.anim:
            self.want_prepare = True      # the layer is still in use
            return
        if self.prepared is not None:
            return
        nxt = self._next_pos()
        item = self._item(nxt)
        if media.kind(item['path']) == 'video':
            self._place(nxt, item, None)
            return
        self.loading = True
        gen = self.gen
        m = self.mon

        def work():
            try:
                surf = render_image(item, m['w'], m['h'], m['scale'])
            except Exception as e:      # never leave the show waiting
                log(f"slideshow: cannot render {item['path']}: {e}")
                surf = None
            GLib.idle_add(done, surf)

        def done(surf):
            if gen != self.gen or not self.loading:
                return False
            self.loading = False
            self._place(nxt, item, surf)
            if self.begin_at is not None:
                self._begin(self.begin_at)
            return False
        threading.Thread(target=work, daemon=True).start()

    def _place(self, nxt, item, surface):
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
        self._load(layer, item, playing=False, surface=surface)
        self.prepared = nxt
        self.desktop.stack.schedule()

    def _begin(self, at=None):
        """Start the transition to the prepared item. at: when it was due,
        which the next one is timed from, so late loads don't drift."""
        if self.static:
            return
        at = self.next_at if at is None else at
        if at is None:
            at = self.clock.now()
        if self.anim:                   # previous one still running
            self.clock.stop(self.anim)
            self._finish()
        if self.prepared is None:
            self.begin_at = at
            self._prepare()
            if self.prepared is None:
                return                  # resumed when the photo is ready
        self.begin_at = None
        inc, out = self.layers[1 - self.cur], self.layers[self.cur]
        item = self._item(self.prepared)
        if isinstance(inc.view, VideoView):
            inc.view.pause(self.hold_videos)
        length = self._length(item)
        self._set_timeline(None if length is None else at + length,
                           media.kind(item['path']) == 'video')
        duration = self._transition_time()
        if self.transition == 'none' or duration <= 0 or self.hold_videos:
            self._finish()              # covered: nobody sees it, just cut
            return
        now = self.clock.now()
        t0 = at if now - at < LATE else now
        W, H = self.mon['w'], self.mon['h']
        kind = self.transition

        def step(now):
            p = min(1.0, max(0.0, (now - t0) / duration))
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
                self.anim = None
                self._finish()
                return False
            return True
        self.anim = step
        self.clock.animate(step)

    def _finish(self):
        self.anim = None
        inc, out = self.layers[1 - self.cur], self.layers[self.cur]
        inc.set_opacity(1.0)
        inc.set_clip(None, 0, 0, 0)
        if isinstance(inc.view, VideoView):
            inc.view.pause(self.hold_videos)
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
        if self.want_prepare:
            self.want_prepare = False
            self._prepare()
