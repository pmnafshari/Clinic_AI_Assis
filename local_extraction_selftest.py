"""Uploaded-note extraction stays on this machine (P13 follow-up).

Every connection attempted during these checks is recorded at the socket layer,
so a request that leaves loopback fails here whatever library made it. No
model runs: each connection is refused after it is recorded, which is also the
"Ollama is down" case that must not fall back to anything else.
"""
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

import extract_note

MARKER = "ZZLOCALMARKER-7f3a"  # stands in for note content; must never leave or be logged


class Recorder:
    """Swap socket.create_connection and socket.socket.connect for a recorder."""

    def __init__(self):
        self.hosts = []

    def __enter__(self):
        self.orig_create = socket.create_connection
        self.orig_connect = socket.socket.connect
        rec = self

        def create_connection(address, *a, **k):
            rec.hosts.append(str(address[0]))
            raise ConnectionRefusedError("recorded, refused")

        def connect(sock, address):
            rec.hosts.append(str(address[0]) if isinstance(address, tuple) else str(address))
            raise ConnectionRefusedError("recorded, refused")

        socket.create_connection = create_connection
        socket.socket.connect = connect
        return self

    def __exit__(self, *exc):
        socket.create_connection = self.orig_create
        socket.socket.connect = self.orig_connect
        return False

    def external(self):
        return [h for h in self.hosts if h not in ("localhost", "127.0.0.1", "::1")]


def selftest():
    saved_env = {k: os.environ.get(k) for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY",
                                                 "https_proxy", "ALL_PROXY", "all_proxy",
                                                 "NO_PROXY", "no_proxy")}
    try:
        # 1. a proxy in the environment does not carry a note off the machine.
        # plain urllib reads proxy variables when it builds its opener, so this
        # runs in a fresh process that starts with them set, as a real one would
        env = dict(os.environ, HTTP_PROXY="http://proxy.invalid:3128",
                   http_proxy="http://proxy.invalid:3128", ALL_PROXY="http://proxy.invalid:3128",
                   all_proxy="http://proxy.invalid:3128")
        for k in ("NO_PROXY", "no_proxy"):
            env.pop(k, None)
        import subprocess
        child = subprocess.run([sys.executable, __file__, "--child-proxy"], env=env,
                               capture_output=True, text=True, timeout=120)
        hosts = json.loads((child.stdout.strip().splitlines() or ["null"])[-1])
        assert hosts, f"1: nothing was attempted at all - the check proves nothing: {child.stderr[-300:]}"
        external = [h for h in hosts if h not in ("localhost", "127.0.0.1", "::1")]
        assert not external, f"1: EXTRACTION TRIED TO REACH {external}"

        # 2. a model endpoint that is not loopback is refused before any
        # connection is made, whatever it is set to
        saved_url = extract_note.OLLAMA_URL
        try:
            for url in ("http://api.example.com/api/generate", "https://10.0.0.5:11434/api/generate",
                        "http://localhost.example.com/api/generate"):
                extract_note.OLLAMA_URL = url
                with Recorder() as rec:
                    try:
                        extract_note.call_model(f"note {MARKER}")
                        raise AssertionError(f"2: {url} was used")
                    except extract_note.OllamaUnreachable:
                        pass
                assert not rec.hosts, f"2: {url}: a connection was attempted: {rec.hosts}"
        finally:
            extract_note.OLLAMA_URL = saved_url

        # 3. the upload worker, the watcher, the typed-note form and the review
        # retry all reach the model through the same local-only call
        import upload_worker
        import watcher
        from app import notes_routes
        assert upload_worker._urlopen is extract_note.local_urlopen, "3: upload worker"
        assert notes_routes._urlopen is extract_note.local_urlopen, "3: typed-note form"
        assert watcher._extract is extract_note.extract_note, "3: watcher"
        # and every other caller that sends clinical text to a model: staff
        # Q&A, the agent, the patient chat
        import inspect

        import agent
        import ask
        import local_model
        from app import agent_routes, qa_routes
        from patient_app import chat
        for owner, fn in ((ask, ask.call_model), (ask, ask.answer_meaning),
                          (agent, agent.call_model), (chat, chat.answer_question),
                          (extract_note, extract_note.call_model)):
            default = inspect.signature(fn).parameters["urlopen"].default
            assert default is local_model.local_urlopen, f"3: {fn.__qualname__} default opener"
        for routes in (agent_routes, qa_routes):
            assert routes._urlopen is local_model.local_urlopen, f"3: {routes.__name__}"
        # 3b. the shared opener itself: proxy set, loopback only, anything else refused
        import urllib.error
        import urllib.request
        with Recorder() as rec:
            for url in ("http://localhost:11434/api/generate", "http://api.example.com/x"):
                try:
                    local_model.local_urlopen(urllib.request.Request(url, data=b"x"), timeout=1)
                except (urllib.error.URLError, OSError):
                    pass
        assert rec.hosts == ["localhost"] or all(h in local_model.LOOPBACK for h in rec.hosts), \
            f"3b: THE SHARED OPENER REACHED {rec.hosts}"
        assert rec.hosts, "3b: the loopback request was never attempted"

        # 4. Ollama down during an upload: the note goes to needs_review, is
        # recorded for a retry, nothing external is tried, and its content is
        # in no log, audit row or error
        import note_review
        import storage
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            saved = (upload_worker.SORTED_ROOT, upload_worker.DB_PATH, upload_worker.CHROMA_PATH,
                     upload_worker.LOG_PATH, note_review.STAGING_ROOT)
            upload_worker.SORTED_ROOT = tmp / "sorted"
            upload_worker.DB_PATH = str(tmp / "c.sqlite")
            upload_worker.CHROMA_PATH = str(tmp / "chroma")
            upload_worker.LOG_PATH = str(tmp / "log.txt")
            note_review.STAGING_ROOT = tmp / "staging"
            saved_extract = upload_worker._extract
            upload_worker._extract = lambda text: extract_note.parse_reply(
                extract_note.call_model(text, urlopen=upload_worker._urlopen))
            storage.init_db(upload_worker.DB_PATH).close()
            drop = tmp / "drop"
            drop.mkdir()
            src = drop / "n.txt"
            src.write_text(f"ZZLC000000000001 {MARKER} otturazione 36")
            try:
                with Recorder() as rec:
                    upload_worker._process_one(str(src), "aassist", "assistant")
            finally:
                upload_worker._extract = saved_extract
            assert not rec.external(), f"4: THE UPLOAD PATH TRIED TO REACH {rec.external()}"
            conn = storage.connect(upload_worker.DB_PATH)
            row = conn.execute("SELECT status, extraction FROM note_reviews").fetchone()
            assert row and row["status"] == "extraction_failed" and row["extraction"] is None, row
            assert conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 0
            dumped = json.dumps([tuple(r) for r in conn.execute("SELECT * FROM audit_log")])
            dumped += json.dumps([tuple(r) for r in conn.execute(
                "SELECT original_name, extraction_error, created_by FROM note_reviews")])
            conn.close()
            assert MARKER not in dumped, "4: note content reached the audit or review rows"
            log = Path(upload_worker.LOG_PATH)
            assert not log.exists() or MARKER not in log.read_text(), "4: note content logged"
            (upload_worker.SORTED_ROOT, upload_worker.DB_PATH, upload_worker.CHROMA_PATH,
             upload_worker.LOG_PATH, note_review.STAGING_ROOT) = saved
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("selftest ok")


def child_proxy():
    """Run inside a process that started with proxy variables set."""
    with Recorder() as rec:
        try:
            extract_note.call_model(f"note {MARKER}")
        except extract_note.OllamaUnreachable as e:
            assert MARKER not in str(e)
    print(json.dumps(rec.hosts))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child-proxy":
        child_proxy()
    elif len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        print("usage: python local_extraction_selftest.py --selftest")
        sys.exit(1)
