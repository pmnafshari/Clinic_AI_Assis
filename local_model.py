"""The one way clinical text reaches a model: on this machine, never via a proxy.

Extraction, staff Q&A, the agent and the patient chat all send clinical text to
Ollama. Plain urllib honours HTTP_PROXY / ALL_PROXY from the environment, so a
proxy variable left in a shell would carry that text to the proxy host (found
2026-09-23; none was set on this machine, and nothing left it). This opener
ignores proxies and refuses any host that is not loopback. There is no remote
fallback anywhere: a refused connection is an error the caller already handles.
"""
import urllib.error
import urllib.parse
import urllib.request

LOOPBACK = ("localhost", "127.0.0.1", "::1")
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_local(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK


def local_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if not is_local(url):
        # URLError, so every caller's existing "model unreachable" path takes it
        raise urllib.error.URLError("refusing a model endpoint that is not on this machine")
    return _NO_PROXY.open(req, timeout=timeout)
