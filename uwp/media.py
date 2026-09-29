"""Library scanning, media probing and cached thumbnails."""
import hashlib
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

from gi.repository import GdkPixbuf, GLib

from .config import CACHE_DIR

IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff',
             '.svg', '.avif', '.heic', '.jxl'}
# Animated GIFs go through the video path so they loop.
VIDEO_EXT = {'.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.wmv',
             '.flv', '.ogv', '.mpg', '.mpeg', '.ts', '.gif'}

THUMB_DIR = os.path.join(CACHE_DIR, 'thumbs')
THUMB_SIZE = 480


def kind(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return 'video'
    if ext in IMAGE_EXT:
        return 'image'
    return None


def scan(entries):
    """Expand library entries (files and folders) into a sorted list of
    (media_path, source_entry)."""
    seen, out = set(), []
    for entry in entries:
        if os.path.isdir(entry):
            for root, dirs, files in os.walk(entry):
                dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
                for name in sorted(files):
                    p = os.path.join(root, name)
                    if kind(p) and p not in seen:
                        seen.add(p)
                        out.append((p, entry))
        elif os.path.isfile(entry) and kind(entry) and entry not in seen:
            seen.add(entry)
            out.append((entry, entry))
    return out


def probe_video(path):
    """Return displayed (width, height) of a video, honoring rotation."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height:stream_side_data=rotation'
             ':stream_tags=rotate:format=duration', '-of', 'json', path],
            capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout or '{}')
        st = data['streams'][0]
        w, h = int(st['width']), int(st['height'])
        rot = 0
        for sd in st.get('side_data_list', []):
            rot = int(float(sd.get('rotation', 0) or 0))
        rot = rot or int(st.get('tags', {}).get('rotate', 0) or 0)
        if abs(rot) % 180 == 90:
            w, h = h, w
        dur = float(data.get('format', {}).get('duration', 0) or 0)
        return w, h, dur
    except (OSError, ValueError, KeyError, IndexError,
            subprocess.SubprocessError):
        return 0, 0, 0.0


def image_size(path):
    fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(path)
    return (w, h) if fmt else (0, 0)


def load_image(path, max_w=0, max_h=0):
    """Load an image with EXIF orientation applied, optionally downscaled."""
    if max_w and max_h:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, max_w, max_h, True)
    else:
        pb = GdkPixbuf.Pixbuf.new_from_file(path)
    return pb.apply_embedded_orientation() or pb


def _cache_key(path):
    st = os.stat(path)
    return hashlib.sha1(
        f'{path}:{st.st_mtime_ns}:{st.st_size}'.encode()).hexdigest()


def make_thumb(path):
    """Blocking: return (pixbuf, real_w, real_h) or None. Results cached."""
    try:
        key = _cache_key(path)
    except OSError:
        return None
    thumb = os.path.join(THUMB_DIR, key + '.png')
    meta = os.path.join(THUMB_DIR, key + '.json')
    try:
        with open(meta) as f:
            m = json.load(f)
        return GdkPixbuf.Pixbuf.new_from_file(thumb), m['w'], m['h']
    except (OSError, ValueError, KeyError, GLib.Error):
        pass
    os.makedirs(THUMB_DIR, exist_ok=True)
    try:
        if kind(path) == 'image':
            pb = load_image(path, THUMB_SIZE, THUMB_SIZE)
            w, h = image_size(path)
            if pb.get_option('orientation') in ('5', '6', '7', '8'):
                w, h = h, w
            if not w:
                w, h = pb.get_width(), pb.get_height()
            pb.savev(thumb, 'png', [], [])
        else:
            if not shutil.which('ffmpeg'):
                return None
            w, h, dur = probe_video(path)
            ss = f'{min(1.0, dur / 3):.2f}' if dur else '0'
            subprocess.run(
                ['ffmpeg', '-v', 'error', '-y', '-ss', ss, '-i', path,
                 '-frames:v', '1', '-vf',
                 f'scale={THUMB_SIZE}:{THUMB_SIZE}:'
                 'force_original_aspect_ratio=decrease', thumb],
                capture_output=True, timeout=30)
            pb = GdkPixbuf.Pixbuf.new_from_file(thumb)
            if not w:
                w, h = pb.get_width(), pb.get_height()
        with open(meta, 'w') as f:
            json.dump({'w': w, 'h': h}, f)
        return pb, w, h
    except (OSError, GLib.Error, subprocess.SubprocessError) as e:
        print(f'uwp: thumbnail failed for {path}: {e}')
        return None


_bg_pool = None


def run_async(fn, args, callback):
    """Run fn(*args) on a worker thread; callback(result) on the main loop."""
    global _bg_pool
    if _bg_pool is None:
        _bg_pool = ThreadPoolExecutor(max_workers=2)

    def done(f):
        try:
            res = f.result()
        except Exception as e:  # never let a worker error kill the UI
            print(f'uwp: background task failed: {e}')
            res = None
        GLib.idle_add(lambda: callback(res) and False)
    _bg_pool.submit(fn, *args).add_done_callback(done)


def video_duration(path):
    """Blocking: length in seconds (0.0 if unknown)."""
    return probe_video(path)[2]


def frame_at(path, t, width=240):
    """Blocking: a single frame at time t (seconds) as a pixbuf, or None."""
    try:
        r = subprocess.run(
            ['ffmpeg', '-v', 'error', '-ss', f'{max(0.0, t):.3f}', '-i', path,
             '-frames:v', '1', '-vf', f'scale={width}:-2', '-f', 'image2pipe',
             '-vcodec', 'png', '-'], capture_output=True, timeout=20)
        if not r.stdout:
            return None
        loader = GdkPixbuf.PixbufLoader.new_with_type('png')
        loader.write(r.stdout)
        loader.close()
        return loader.get_pixbuf()
    except (OSError, GLib.Error, subprocess.SubprocessError):
        return None


class Thumbnailer:
    """Generates thumbnails off the main thread; callbacks run on it.

    cache=False skips the in-memory cache (the browser keeps its own
    scaled copies, and folders can hold thousands of images)."""

    def __init__(self, cache=True):
        self._pool = ThreadPoolExecutor(max_workers=3)
        self._use_cache = cache
        self._cache = {}      # path -> (pixbuf, w, h) | None
        self._waiting = {}    # path -> [callbacks]
        self._futures = {}    # path -> Future still queued or running

    def get(self, path):
        return self._cache.get(path)

    def request(self, path, callback):
        if path in self._cache:
            callback(path, self._cache[path])
            return
        if path in self._waiting:
            self._waiting[path].append(callback)
            return
        self._waiting[path] = [callback]
        fut = self._pool.submit(make_thumb, path)
        self._futures[path] = fut
        fut.add_done_callback(
            lambda f: GLib.idle_add(self._done, path, f))

    def _done(self, path, fut):
        self._futures.pop(path, None)
        callbacks = self._waiting.pop(path, [])
        if fut.cancelled():
            return False
        result = fut.result()
        if self._use_cache:
            self._cache[path] = result
        for cb in callbacks:
            cb(path, result)
        return False

    def cancel_pending(self):
        """Drop queued (not yet started) work, e.g. when leaving a folder."""
        for fut in list(self._futures.values()):
            fut.cancel()

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
