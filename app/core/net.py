"""The client's IP address, as the one place that decides which X-Forwarded-For hop to trust.

Behind Coolify/Traefik, REMOTE_ADDR is the proxy, so the real client is in X-Forwarded-For. Only the
LAST hop is trustworthy: it is the one Traefik appended (the address it actually saw). Everything to
its left arrived in the request's own header, so a client can put anything there — taking the first
hop let any visitor pick their own IP. Safe only because the app server is reachable *only* via the
proxy. Used by the ølkælder kiosk gate and the front-page visit counter, which used to disagree.
"""

from django.http import HttpRequest


def client_ip(request: HttpRequest) -> str:
    """The client's IP: the last X-Forwarded-For hop, else REMOTE_ADDR ("" when neither is set)."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")
