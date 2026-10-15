// UWP's wallpapers are X11 desktop-type windows (XWayland on Wayland).
//
// Desktop Icons NG makes its own windows desktop-type too and lowers them to
// the bottom whenever anything happens: a click on the desktop, any window
// mapped or closed (each slideshow change maps one of ours and unmaps
// another). Fighting that by lowering ours again after every restack always
// left a frame with the icons hidden under the wallpaper. Instead, this runs
// inside GNOME Shell and moves our window actors into a background group of
// their own, just above GNOME's background. Mutter keeps background groups
// below every window actor, so the icons are drawn on top (and get the
// clicks) whatever the window stacking order is. Mutter still orders our
// windows among themselves, which the slideshow layers rely on.
//
// It also answers which monitors are covered by maximized or fullscreen
// windows (D-Bus, below), since GNOME doesn't list Wayland windows to apps.
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const WALLPAPER_TITLE = 'UWP wallpaper ';   // renderer.MonitorWindow
const OBJECT_PATH = '/io/github/RegulusArms/UWP/Helper';
const IFACE = `<node>
  <interface name="io.github.RegulusArms.UWP.Helper">
    <property name="Version" type="u" access="read"/>
    <method name="GetCovered">
      <arg type="as" direction="in" name="connectors"/>
      <arg type="as" direction="out" name="covered"/>
    </method>
  </interface>
</node>`;

function isWallpaper(w) {
    return w.get_client_type() === Meta.WindowClientType.X11 &&
        w.get_window_type() === Meta.WindowType.DESKTOP &&
        (w.get_title() ?? '').startsWith(WALLPAPER_TITLE);
}

function isMaximized(w) {
    // GNOME 49 replaced get_maximized() with is_maximized().
    return w.is_maximized?.() ??
        w.get_maximized() === Meta.MaximizeFlags.BOTH;
}

export default class UwpHelper extends Extension {
    enable() {
        // A Meta.BackgroundGroup, so mutter's stack sync keeps it with the
        // backgrounds, under all windows.
        this._group = new Meta.BackgroundGroup({name: 'uwp-wallpapers'});
        global.window_group.insert_child_above(this._group,
            Main.layoutManager._backgroundGroup);
        // 'map' comes before the window's first frame is drawn; the others
        // catch anything mapped before enable() or retitled later.
        global.window_manager.connectObject(
            'map', (_wm, actor) => this._adopt(actor.get_meta_window()), this);
        global.display.connectObject(
            'window-created', (_d, w) => this._adopt(w),
            'restacked', () => this._adoptAll(), this);
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._adoptAll();
    }

    disable() {
        global.window_manager.disconnectObject(this);
        global.display.disconnectObject(this);
        this._dbus.unexport();
        this._dbus = null;
        for (const actor of [...this._group.get_children()]) {
            this._group.remove_child(actor);
            global.window_group.add_child(actor);
        }
        this._group.destroy();
        this._group = null;
    }

    get Version() {
        return 3;
    }

    _adopt(w) {
        if (!w || !isWallpaper(w))
            return;
        const actor = w.get_compositor_private();
        const parent = actor?.get_parent();
        if (!parent || parent === this._group)
            return;
        parent.remove_child(actor);
        this._group.add_child(actor);
    }

    _adoptAll() {
        for (const w of global.display.list_all_windows())
            this._adopt(w);
    }

    // Same rule as renderer.CoverWatcher: a monitor is covered when a
    // maximized/fullscreen window fills half of it, or any window 90%.
    GetCovered(connectors) {
        const mm = global.backend.get_monitor_manager();
        const ws = global.workspace_manager.get_active_workspace();
        const wins = global.display.list_all_windows().filter(w =>
            w.get_window_type() === Meta.WindowType.NORMAL &&
            !w.minimized && w.located_on_workspace(ws));
        return connectors.filter(connector => {
            const i = mm.get_monitor_for_connector(connector);
            if (i < 0)
                return false;
            const m = global.display.get_monitor_geometry(i);
            return wins.some(w => {
                const r = w.get_frame_rect();
                const ix = Math.max(0, Math.min(r.x + r.width, m.x + m.width) -
                    Math.max(r.x, m.x));
                const iy = Math.max(0, Math.min(r.y + r.height, m.y + m.height) -
                    Math.max(r.y, m.y));
                const full = w.is_fullscreen() || isMaximized(w);
                return ix * iy >= (full ? 0.5 : 0.9) * m.width * m.height;
            });
        });
    }
}
