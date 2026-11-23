"""The address a request came from, as the proxy in front of the app saw it."""

from django.http import HttpRequest


def client_ip(request: HttpRequest) -> str:
    """The client's real IP. Behind Coolify/Traefik, REMOTE_ADDR is the proxy, so trust the last hop of
    X-Forwarded-For (the IP Traefik observed — the rightmost entry is the one it appended, not a value
    a client could spoof). Safe only because the app server is reachable *only* via the proxy."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")
