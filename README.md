# UWP: lightweight video and image wallpapers for GNOME

A small, Wallpaper Engine–style wallpaper manager for Ubuntu. It lives in the
top bar, puts a different image or looping video on each monitor, and uses
the GPU for decoding and transforms so it stays light on CPU.

## Features

- **Per-monitor wallpapers**: images or videos, each with its own scale mode
  (fill, fit, stretch, center), zoom, X/Y shift and rotation.
- **Visual editor**: a picture of your monitor layout with live previews.
  Drag to move, scroll to zoom, Shift+scroll to rotate.
- **Live preview on desktop**: see unsaved changes on the real monitors as
  you edit. OK keeps them, Cancel restores the old wallpapers.
- **Video controls**: playback speed from 0.1× to 4×, and loop a chosen section
  (two-handle timeline or exact times, with start/end frame previews).
- **Slideshows**: any mix of photos and videos per monitor. Each item keeps
  its own placement, speed and loop section. Photos show for a set time
  (per item or a default), and videos play their full length or loop section
  a chosen number of times. Transitions: crossfade, fade through black,
  wipes (left, right, up, down), cut or random, with an adjustable
  length. Shuffle is optional.
- **Profiles**: save whole-desktop setups and switch between them from the
  top bar or with a global keyboard shortcut (next, previous, or a specific
  profile).
- **Thumbnail file browser** for building your library, with an adjustable
  thumbnail size.
- **Desktop icons stay usable**: icons from the Desktop Icons NG extension
  draw on top of the wallpaper and stay clickable.
- **Light on resources**: images cost nothing once drawn, videos use
  hardware decoding (NVDEC, VA-API) and are never audio-decoded, and videos
  pause while a maximized or fullscreen window covers them.

## Requirements

- Ubuntu 24.04 LTS with GNOME on **X11** (the "Ubuntu on Xorg" login option).
  Wayland is untested: UWP forces XWayland, but desktop-window stacking and
  the pause-when-covered check rely on X11.
- The Ubuntu AppIndicator extension (enabled by default on Ubuntu) for the
  top-bar icon.
- `install.sh` installs any missing packages with apt: GTK 3 and GStreamer
  introspection, GStreamer GL/good/bad/libav plugins, ffmpeg, libwnck and
  AyatanaAppIndicator.

## Install

```sh
git clone <this repo> ~/UWP
cd ~/UWP
./install.sh
```

This adds a `uwp` command (`~/.local/bin/uwp`), a "UWP Wallpapers" entry in
the app grid, and starts UWP in the top bar. To start it at login, turn on
**Start UWP when I log in** under *Shortcuts & settings*.

```sh
./reinstall.sh            # reinstall, keeping settings (--clean to start fresh)
./uninstall.sh            # remove, keeping settings (--purge to delete them too)
```

## Usage

1. Click the top-bar icon and choose **Open UWP…** (or run `uwp --open`).
2. **Add files… / Add folder…** to build your library.
3. Click a monitor in the layout (Ctrl+click for several), then click a
   wallpaper in the library.
4. Adjust it on the **Placement** and **Video** tabs. Turn on **Live preview
   on desktop** to see it on the real monitors.
5. Press **OK** to apply. Cancel or Esc discards everything.

Command line:

```
uwp --open               open the settings window
uwp --background         start in the top bar only
uwp --next-profile       switch to the next profile (also --prev-profile)
uwp --profile NAME       switch to a profile by name or id
uwp --quit               stop UWP
uwp --foreground ...     run attached to the terminal (debugging)
```

The first `uwp` starts the app detached from the terminal, so closing the
terminal won't stop it. Later calls talk to the running instance.

## Where things are stored

Nothing personal is written into this folder:

| What | Where |
| --- | --- |
| Library, profiles, settings | `~/.config/uwp/config.json` |
| Thumbnails, log | `~/.cache/uwp/` |
| Keyboard shortcuts | GNOME custom shortcuts (Settings → Keyboard) |
| Start at login | `~/.config/autostart/io.github.RegulusArms.UWP.desktop` |

## How it works

- One desktop-type X11 window per monitor, kept underneath the desktop-icon
  windows.
- Images are drawn once with Cairo.
- Videos play through GStreamer: hardware decoding, then `gltransformation`
  for scale and rotation on the GPU, then `glimagesink`. Looping uses
  segment seeks, so it's seamless and never rebuilds the pipeline.
- Profile shortcuts are GNOME custom shortcuts that call the running app's
  D-Bus actions with `gdbus`.

Measured on an RTX 3090: two 1080p30 videos use about 17% of one CPU core in
total.

## Known limitations

- Built and tested on GNOME/X11 only.
- Don't use Ctrl+Alt+F1–F12 or Ctrl+Alt+Backspace/Delete as shortcuts: the
  system acts on those before GNOME (for example, switching to a text
  console). UWP refuses them.
- Troubleshooting: check `~/.cache/uwp/uwp.log`, or run
  `uwp --quit; uwp --foreground --background` to see output live.

## License

MIT. See [LICENSE](LICENSE).
