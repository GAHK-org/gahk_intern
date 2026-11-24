"""Hjemmeside's live topics: one per thing an editor can have open — "cms.page-41", "cms.news-3",
"cms.event-7" and "cms.menu". See core.live for the design.

A signal says that the thing was saved or deleted, or that somebody opened or left its editor. The
editor's box about the others (templates/cms/_editing.html) answers every one of them the same way,
by reading itself again; only a deletion is shown without asking.
"""

import re
from enum import StrEnum

from django.http import HttpRequest

from core import live
from residents.permissions import CMS_EDITOR_ROLES, request_has_role

FEATURE = "cms"

# What an editor is open on: a page, news item or event by its kind and id, or the one menu.
TARGET = re.compile(r"^(?:(?:page|news|event)-[1-9][0-9]{0,17}|menu)$")


class Kind(StrEnum):
    """What an editor can be open on. Each is also the change log's "type" filter for it, and
    "cms.<kind>" is its kind in the audit trail."""

    PAGE = "page"
    NEWS = "news"
    EVENT = "event"
    MENU = "menu"


def target(kind: str, pk: int | None = None) -> str:
    """The editor's name for a thing: "page-41" for a page, "menu" for the menu (`kind` a Kind)."""
    return kind if pk is None else f"{kind}-{pk}"


def topic(target: str) -> str:
    return f"{FEATURE}.{target}"


def may_listen(request: HttpRequest, rest: str) -> bool:
    return bool(TARGET.match(rest)) and request_has_role(request, *CMS_EDITOR_ROLES)


def changed(target: str, pk: int | None) -> None:
    live.publish(topic(target), "changed", pk)


def deleted(target: str, pk: int) -> None:
    live.publish(topic(target), "deleted", pk)


def register() -> None:
    live.register(FEATURE, may_listen)
