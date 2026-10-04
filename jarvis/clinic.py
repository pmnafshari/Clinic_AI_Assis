"""Jarvis's only door into the clinic app: the device credential, sent over loopback, never logged or echoed."""
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_KEY = Path("~/.config/clinic-demo/jarvis-device").expanduser()
TIMEOUT = 5


class LinkDown(Exception):
    pass


class LinkRefused(Exception):
    pass


def read_key(path):
    path = Path(path)
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"{path} must be readable by its owner only (chmod 600)")
    return path.read_text().strip()


def store_key(path, token):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token.strip() + "\n")
    os.chmod(path, 0o600)


class ClinicLink:
    def __init__(self, base_url, key_path=DEFAULT_KEY):
        self.base_url = base_url.rstrip("/")
        self._token = read_key(key_path)

    def whoami(self):
        req = urllib.request.Request(f"{self.base_url}/api/jarvis/whoami",
                                     headers={"Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise LinkRefused("device not registered or revoked") from None
            raise LinkDown(f"clinic app answered {e.code}") from None
        except (urllib.error.URLError, OSError):
            raise LinkDown("clinic app unreachable") from None
