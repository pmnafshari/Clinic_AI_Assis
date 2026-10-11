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


class Refused(Exception):
    """The clinic refused a note (J03) or a sign-in: `code` says why - auth_required, role, empty, no_match,
    too_many, patient, gone; credentials, not_doctor, password_change."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


NoteRefused = Refused


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

    def _send(self, req):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise LinkRefused("device not registered or revoked") from None
            raise LinkDown(f"clinic app answered {e.code}") from None
        except (urllib.error.URLError, OSError):
            raise LinkDown("clinic app unreachable") from None

    def _post(self, path, payload):
        req = urllib.request.Request(f"{self.base_url}{path}", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise LinkRefused("device not registered or revoked") from None
            try:
                body = json.loads(e.read())
            except (ValueError, OSError):
                body = {}
            if isinstance(body, dict) and isinstance(body.get("error"), str) and e.code in (400, 403, 404, 409, 410):
                raise Refused(body["error"], body.get("message", "")) from None
            raise LinkDown(f"clinic app answered {e.code}") from None
        except (urllib.error.URLError, OSError):
            raise LinkDown("clinic app unreachable") from None

    def note_draft(self, patient, text):
        """A note heard for a spoken patient name (J03) -> {draft, choices, text, seconds}; nothing is written."""
        return self._post("/api/jarvis/notes/draft", {"patient": patient, "text": text})

    def note_commit(self, draft, cf):
        """Save a drafted note on the patient picked on screen -> {saved, cf, name, status}."""
        return self._post("/api/jarvis/notes/commit", {"draft": draft, "cf": cf})

    def login(self, username, password):
        """A doctor signs this device in until the next 00:00 (the clinic checks; nothing is kept here)."""
        return self._post("/api/jarvis/login", {"username": username, "password": password})

    def logout(self, why="signed_out"):
        req = urllib.request.Request(f"{self.base_url}/api/jarvis/logout", data=json.dumps({"why": why}).encode(),
                                     method="POST", headers={"Authorization": f"Bearer {self._token}",
                                                             "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT):
                return
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise LinkRefused("device not registered or revoked") from None
            raise LinkDown(f"clinic app answered {e.code}") from None
        except (urllib.error.URLError, OSError):
            raise LinkDown("clinic app unreachable") from None

    def whoami(self):
        return self._send(urllib.request.Request(f"{self.base_url}/api/jarvis/whoami",
                                                 headers={"Authorization": f"Bearer {self._token}"}))

    def vocabulary(self):
        """The approved library's own terms, the speech-to-text hint (J-D10)."""
        return self._send(urllib.request.Request(f"{self.base_url}/api/jarvis/guides/vocabulary",
                                                 headers={"Authorization": f"Bearer {self._token}"}))["terms"]

    def ask_guides(self, question):
        """A clinic-guide question under this device's guide scope (J02) -> the answer or refusal."""
        return self._send(urllib.request.Request(
            f"{self.base_url}/api/jarvis/guides/ask", data=json.dumps({"question": question}).encode(),
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}, method="POST"))
