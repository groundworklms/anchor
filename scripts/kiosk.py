#!/usr/bin/env python3
"""Full-screen kiosk for the doctrine tutor.

Why not a browser: the Orin has no internet, so nothing can be installed at the venue,
and Chromium on Ubuntu 22.04 arm64 is snap-only -- snapd was disabled to protect the
60-second cold-boot budget. libwebkit2gtk and the GObject bindings are already present
on the image, so the kiosk is thirty lines against a library that is already there.

It is also a better kiosk than a browser would be: no address bar to type into, no tab
strip, no update prompt mid-demo, and no way for a judge holding the keyboard to
navigate away from the tutor by accident.

    python3 kiosk.py [url]
"""
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.0")
from gi.repository import GLib, Gtk, WebKit2                        # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/"
RETRY_S = 2


class Kiosk(Gtk.Window):
    def __init__(self, url):
        super().__init__(title="Doctrine Tutor")
        self.url = url
        self.view = WebKit2.WebView()

        # A kiosk that offers "Reload" and "Inspect Element" on right-click is a laptop
        # with the lid open, not an appliance.
        self.view.connect("context-menu", lambda *a: True)
        self.view.connect("load-failed", self.on_failed)

        st = self.view.get_settings()
        st.set_enable_developer_extras(False)
        st.set_enable_back_forward_navigation_gestures(False)

        self.add(self.view)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self.on_key)
        self.fullscreen()
        self.view.load_uri(self.url)

    def on_failed(self, view, event, uri, error):
        """The kiosk may win the race against the API on a cold boot.

        Retrying rather than showing WebKit's error page matters: at boot the units come
        up in parallel, and a judge watching the screen should see the tutor appear, not
        a browser error that they have to be told to ignore.
        """
        GLib.timeout_add_seconds(RETRY_S, self.retry)
        return True

    def retry(self):
        self.view.load_uri(self.url)
        return False

    def on_key(self, widget, event):
        # Ctrl+Shift+Q only. Deliberately awkward: nothing a stray keypress can hit.
        ctrl_shift = 0x04 | 0x01
        if event.keyval in (ord("q"), ord("Q")) and (event.state & ctrl_shift) == ctrl_shift:
            Gtk.main_quit()
            return True
        return False


if __name__ == "__main__":
    Kiosk(URL).show_all()
    Gtk.main()
