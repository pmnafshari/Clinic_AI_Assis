"""Jarvis's persistent menu-bar indicator (J01). It only shows what the service reports; if the service cannot be
reached it says so - it never shows READY on its own."""
import json
import subprocess
import sys
import urllib.request

STATUS = "http://127.0.0.1:5020/status"
MARK = {"STARTING": "◌", "READY": "●", "ACTIVE": "◉", "WAITING_FOR_CONFIRMATION": "◎", "AUTH_REQUIRED": "◆",
        "CONFIRMING": "◇", "DEGRADED": "✕"}


def label(status):
    if not status:
        return "Jarvis ✕ not running"
    return f"Jarvis {MARK.get(status['state'], '?')} {status['state']}"


def fetch():
    try:
        with urllib.request.urlopen(STATUS, timeout=1.5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def main():
    import rumps

    class App(rumps.App):
        def __init__(self):
            super().__init__("Jarvis", title=label(None), quit_button="Quit indicator")
            self.why = rumps.MenuItem("starting")
            self.link = rumps.MenuItem("clinic link: unknown")
            self.menu = [self.why, self.link, None, rumps.MenuItem("Open Jarvis page", callback=self.open_page)]

        @rumps.timer(2)
        def refresh(self, _):
            s = fetch()
            self.title = label(s)
            self.why.title = s["reason"] if s else "the Jarvis service is not running"
            self.link.title = f"clinic link: {s['clinic']['detail']}" if s and s.get("clinic") else "clinic link: unknown"

        def open_page(self, _):
            subprocess.run(["open", "http://127.0.0.1:5020"], check=False)
    App().run()


if __name__ == "__main__":
    sys.exit(main())
