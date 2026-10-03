// UWP's wallpapers are X11 desktop-type windows (XWayland on Wayland).
//
// On Wayland, Desktop Icons NG makes its own windows desktop-type too and
// lowers them to the bottom every time they are raised, i.e. on every click,
// which drops the icons and their menus under the wallpaper. UWP can't see
// Wayland windows to undo that, so this runs inside GNOME Shell: after any
// restack, before the next frame is drawn, it moves UWP's windows back to the
// very bottom. On X11 UWP does this itself; this just does it sooner.
//
// It also answers which monitors are covered by maximized or fullscreen
// windows (D-Bus, below), since GNOME doesn't list Wayland windows to apps.
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
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

function laters() {
    return global.compositor.get_laters();
}

export default class UwpHelper extends Extension {
    enable() {
        this._later = 0;
        global.display.connectObject('restacked', () => this._queue(), this);
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._queue();
    }

    disable() {
        global.display.disconnectObject(this);
        if (this._later)
            laters().remove(this._later);
        this._later = 0;
        this._dbus.unexport();
        this._dbus = null;
    }

    get Version() {
        return 1;
    }

    _queue() {
        if (this._later)
            return;
        this._later = laters().add(Meta.LaterType.BEFORE_REDRAW, () => {
            this._later = 0;
            this._restack();
            return false;
        });
    }

    _restack() {
        const desktop = global.display.sort_windows_by_stacking(
            global.display.list_all_windows()).filter(
            w => w.get_window_type() === Meta.WindowType.DESKTOP);
        const ours = desktop.filter(isWallpaper);   // bottom to top
        const foreign = desktop.findIndex(w => !isWallpaper(w));
        if (foreign < 0 || foreign >= ours.length)
            return;   // ours are already the bottom-most desktop windows
        // Lowering each to the bottom, top one first, keeps their order.
        // That restacks again, and the check above then finds nothing to do.
        for (const w of ours.reverse())
            w.lower();
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
