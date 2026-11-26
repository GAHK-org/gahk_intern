"""Limits on the two forms anybody can post to without an account: logging in and "Glemt kodeord".

Without them, the login form takes as many password guesses as a script can send, and the reset form
mails a resident as often as somebody asks it to. Every one of those mails goes out through one.com,
which throttles the whole SMTP account after about 25 in five minutes — so a loop on the reset form
would also hold up every other mail the site sends.

Counted in Django's cache, each count in a fixed window that starts with its first attempt. The
cache is the web process's own memory (no CACHES setting), which is exactly right while `web` is one
process (DEPLOY.md §4); a restart forgets the counts, which costs nothing worth keeping.

THE DORM IS ONE IP. All 61 residents reach the site through the dorm's single public address, so a
limit per IP is a limit on the whole dorm at once, and the numbers below are sized for that:

  * A login's address is counted together with the IP, not on its own. Counted alone, anybody could
    lock a resident out from anywhere by typing their address wrong ten times.
  * The IP's own login limit only catches one source guessing across many addresses, and leaves
    every resident about five typos in the same quarter hour before it touches anybody.
  * The IP's reset limit lets every resident ask for a link in the same hour (a move-in evening).
    There is deliberately no site-wide cap: it would drop real residents' links without a word,
    while a burst that does reach one.com's throttle only delays them (core.mail waits and retries).
"""

import hashlib
import logging
import unicodedata
from datetime import timedelta

from django.core.cache import cache
from django.http import HttpRequest

from core.client_ip import client_ip

logger = logging.getLogger(__name__)

# Residents behind the dorm's one public IP; the per-IP limits are sized from it.
RESIDENTS_SHARING_AN_IP = 61

LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_FAILURES_PER_ADDRESS = 10  # from one IP
LOGIN_FAILURES_PER_IP = 5 * RESIDENTS_SHARING_AN_IP

RESET_WINDOW = timedelta(hours=1)
RESETS_PER_ADDRESS = 3
RESETS_PER_IP = RESIDENTS_SHARING_AN_IP


def _normalise(address: str | None) -> str:
    return unicodedata.normalize("NFKC", address or "").strip().casefold()


def _key(kind: str, *parts: str) -> str:
    # Hashed: cache keys have a length and character limit (memcached), and addresses need not fit it.
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    return f"throttle:{kind}:{digest}"


def _count(key: str) -> int:
    return cache.get(key, 0)


def _hit(key: str, window: timedelta) -> int:
    """Count one more in `key`'s window, starting the window if this is its first."""
    cache.add(key, 0, int(window.total_seconds()))
    try:
        return cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, int(window.total_seconds()))
        return 1


def _login_keys(request: HttpRequest, address: str | None) -> tuple[str, str]:
    ip = client_ip(request)
    return _key("login-address", ip, _normalise(address)), _key("login-ip", ip)


def login_blocked(request: HttpRequest, address: str | None) -> bool:
    """Too many failed logins lately for this address from here, or from here at all."""
    address_key, ip_key = _login_keys(request, address)
    blocked = _count(address_key) >= LOGIN_FAILURES_PER_ADDRESS or _count(ip_key) >= LOGIN_FAILURES_PER_IP
    if blocked:
        logger.warning("Login refused: too many failed attempts from %s", client_ip(request))
    return blocked


def login_failed(request: HttpRequest, address: str | None) -> None:
    for key in _login_keys(request, address):
        _hit(key, LOGIN_WINDOW)


def login_succeeded(request: HttpRequest, address: str | None) -> None:
    """A resident who got it right on the ninth try starts from nothing next time."""
    cache.delete(_login_keys(request, address)[0])


def password_reset_allowed(request: HttpRequest, address: str) -> bool:
    """Count one reset request, and say whether it may send its mail."""
    ip = client_ip(request)
    per_address = _hit(_key("reset-address", _normalise(address)), RESET_WINDOW)
    per_ip = _hit(_key("reset-ip", ip), RESET_WINDOW)
    if per_address > RESETS_PER_ADDRESS or per_ip > RESETS_PER_IP:
        logger.warning("Password reset not sent: too many requests (from %s)", ip)
        return False
    return True
