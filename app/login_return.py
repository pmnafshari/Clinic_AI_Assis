from urllib.parse import unquote, urlsplit

from flask import current_app, request, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.routing import RoutingException


def local_destination(value):
    """Only registered local GET pages can be resumed after authentication."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return ""
    if any(char.isspace() for char in value):
        return ""
    decoded = value
    for _ in range(5):
        if any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in decoded):
            return ""
        try:
            parts = urlsplit(decoded)
        except ValueError:
            return ""
        if parts.scheme or parts.netloc or not decoded.startswith("/") or decoded.startswith("//"):
            return ""
        if any(segment in (".", "..") for segment in parts.path.split("/")):
            return ""
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    else:
        return ""
    try:
        endpoint, _ = current_app.url_map.bind("localhost").match(parts.path, method="GET")
    except (HTTPException, RoutingException):
        return ""
    if (endpoint in ("static", "shared", "jarvis.handoff")
            or endpoint.startswith(("auth.", "jarvis.api_"))
            or parts.path.startswith("/api/")):
        return ""
    return value


def authentication_url(endpoint):
    # Never retain or replay the body of an expired write.
    target = local_destination(request.full_path.rstrip("?")) if request.method == "GET" else ""
    return url_for(endpoint, **({"next": target} if target else {}))
