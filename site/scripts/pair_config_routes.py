"""Path classification for the pair-config HTTP adapter.

Public routes skip auth. Everything else is a user route dispatched to the
existing Handler methods. Per-path bodies stay in the Handler; this module
owns which method runs.
"""

from __future__ import annotations

_PUBLIC = {
    ("GET", "/health"): "health",
    ("POST", "/auth/login"): "login",
    ("POST", "/auth/telegram-webapp"): "telegram",
}


def classify(method: str, path: str) -> tuple[str, str]:
    """Return ``(audience, target)``.

    audience is ``public`` or ``user``.
    target is ``health``, ``login``, ``telegram``, ``bot_proxy``,
    ``get``, ``post``, ``put``, ``patch``, ``delete``, or ``method_not_allowed``.
    """
    verb = str(method or "").upper()
    route = path or "/"
    public = _PUBLIC.get((verb, route))
    if public:
        return "public", public
    if route.startswith("/bot-proxy/") or route == "/bot-proxy":
        return "user", "bot_proxy"
    if verb == "GET":
        return "user", "get"
    if verb == "POST":
        return "user", "post"
    if verb == "PUT":
        return "user", "put"
    if verb == "PATCH":
        return "user", "patch"
    if verb == "DELETE":
        return "user", "delete"
    return "user", "method_not_allowed"
