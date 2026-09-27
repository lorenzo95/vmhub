from __future__ import annotations

CSS = b"""
window { background: @theme_bg_color; }
.vmlist { padding: 6px; }
.vmrow { padding: 8px 10px; border-radius: 8px; margin-bottom: 2px; }
.vmrowhost { border-radius: 8px; }
.vmrowhost:hover { background: alpha(currentColor, 0.06); }
.vmrowhost.selected { background: alpha(@accent_bg_color, 0.20); }
.vmname { font-weight: 700; font-size: 1.05em; }
.vmmeta { font-size: 0.82em; opacity: 0.62; }
.dot { border-radius: 50%; min-width: 10px; min-height: 10px; margin-right: 8px; }
.badge { font-size: 0.72em; padding: 1px 7px; border-radius: 9px; margin-right: 5px; }
.badge.template { background: #2b6cb0; color: #ffffff; }
.badge.clone { background: #6b46c1; color: #ffffff; }
.badge.snap { background: #2f855a; color: #ffffff; }
.section { font-weight: 700; font-size: 0.95em; opacity: 0.75; margin: 14px 0 6px 0; }
.kv { font-size: 0.9em; }
.kvkey { opacity: 0.6; }
.mono { font-family: monospace; font-size: 0.85em; }
.dim { opacity: 0.6; }
.big { font-size: 1.6em; font-weight: 700; }
.logview { font-family: monospace; font-size: 0.8em; padding: 8px; }
.hint { opacity: 0.6; font-size: 0.86em; }
.warnrow { color: @warning_color; }
.errrow { color: @error_color; }
.bigbutton { padding: 14px 22px; font-size: 1.05em; font-weight: 600; }
.toolbar { padding: 6px 10px; border-bottom: 1px solid alpha(currentColor,0.12); }
"""

STATE_COLOURS = {
    "running": "#38a169",
    "paused": "#d69e2e",
    "stopped": "#718096",
    "exited": "#e53e3e",
    "absent": "#718096",
    "unknown": "#718096",
    "missing": "#e53e3e",
}


def apply(app) -> None:
    from gi.repository import Gtk

    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    display = __import__("gi.repository.Gdk", fromlist=["Gdk"]).Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


def state_colour(state: str) -> str:
    return STATE_COLOURS.get(state, "#718096")
