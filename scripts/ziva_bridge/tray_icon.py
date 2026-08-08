#!/usr/bin/env python3
"""Top-bar status indicator for the ziva WhatsApp bridge
(~/DevOps_Files/scripts/ziva_bridge). Same pattern as the personal bridge's
own tray_icon.py (~/MISC/whatsapp_bridge/tray_icon.py), kept as a separate
indicator instance since both bridges can run at the same time and need to
be told apart at a glance -- icons carry a "Z" marker, and process matching
uses this bridge's absolute index.js path rather than the bare
"node index.js" command, which both bridges share and can't be told apart
by alone."""
import gi
gi.require_version('AppIndicator3', '0.1')
gi.require_version('Gtk', '3.0')
from gi.repository import AppIndicator3, Gtk, GLib
import subprocess
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_JS = os.path.join(SCRIPT_DIR, 'index.js')
CHECK_INTERVAL_SECONDS = 5
ICON_ACTIVE = 'icon_active'
ICON_STOPPED = 'icon_stopped'


def bridge_is_running():
    # Match on this bridge's own absolute path, not the bare "node index.js"
    # command -- both bridges share that exact command line, so a bare match
    # would report ziva as running whenever the personal bridge is, and vice
    # versa.
    result = subprocess.run(
        ['pgrep', '-f', INDEX_JS],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def stop_bridge():
    subprocess.run(['pkill', '-f', INDEX_JS])


def start_bridge():
    log_path = os.path.join(SCRIPT_DIR, 'run.log')
    with open(log_path, 'ab') as log_file:
        subprocess.Popen(
            ['setsid', 'node', INDEX_JS],
            cwd=SCRIPT_DIR,
            stdout=log_file, stderr=log_file,
            start_new_session=True,
        )


def on_toggle_clicked(_item):
    if bridge_is_running():
        stop_bridge()
    else:
        start_bridge()


def build_menu():
    menu = Gtk.Menu()

    status_item = Gtk.MenuItem(label='Checking...')
    status_item.set_sensitive(False)
    menu.append(status_item)

    menu.append(Gtk.SeparatorMenuItem())

    toggle_item = Gtk.MenuItem(label='...')
    toggle_item.connect('activate', on_toggle_clicked)
    menu.append(toggle_item)

    menu.append(Gtk.SeparatorMenuItem())

    quit_item = Gtk.MenuItem(label='Quit indicator (leaves bridge as-is)')
    quit_item.connect('activate', lambda _: Gtk.main_quit())
    menu.append(quit_item)

    menu.show_all()
    return menu, status_item, toggle_item


def refresh(indicator, status_item, toggle_item):
    running = bridge_is_running()
    if running:
        indicator.set_icon_full(ICON_ACTIVE, 'ziva bridge active')
        status_item.set_label('ziva bridge: ACTIVE')
        toggle_item.set_label('Stop ziva bridge')
    else:
        indicator.set_icon_full(ICON_STOPPED, 'ziva bridge stopped')
        status_item.set_label('ziva bridge: STOPPED')
        toggle_item.set_label('Start ziva bridge')
    indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
    return True


def main():
    indicator = AppIndicator3.Indicator.new(
        'ziva-bridge-status',
        ICON_STOPPED,
        AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
    )
    indicator.set_icon_theme_path(SCRIPT_DIR)
    indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

    menu, status_item, toggle_item = build_menu()
    indicator.set_menu(menu)

    refresh(indicator, status_item, toggle_item)
    GLib.timeout_add_seconds(CHECK_INTERVAL_SECONDS, refresh, indicator, status_item, toggle_item)

    Gtk.main()


if __name__ == '__main__':
    main()
