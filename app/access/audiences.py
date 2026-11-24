"""Who sees a thing: an audience is a set of roles, that is of groups (access.roles), carried by the
thing itself.

A Den Hurtige channel, an opslag and an open event each say who they are for. Someone sees one when
any of their roles is in its audience, and the feature is open to them as well (access.policy): the
audience narrows who, the permissions still decide what. So "only the kitchen's channel" is the
other channels leaving the kitchen out, not a rule about the kitchen in each app, and a channel or a
post for former residents only is an audience of their stages.

Stored as a list of role codes, which are the groups' names (an ArrayField: no join table per post,
and a default the database fills), and offered to people as a few named presets. Administrators are
in every audience: they moderate what they could not otherwise see.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q, QuerySet

from residents.models import Account

from .policy import ADMINISTRATOR, KITCHEN, ROLE_LABELS
from .roles import holding
from .stages import Stage

Audience = frozenset[str]

# Everyone with a stage: living here, moving in, moved out (each as far as their stage's
# permissions reach). Superusers are residents, so they are in it too.
HOUSE: Audience = frozenset(stage.value for stage in Stage)
# Living here, or about to.
LIVING: Audience = frozenset({Stage.MOVING_IN.value, Stage.RESIDENT.value})
# Those who have moved out.
FORMER: Audience = HOUSE - LIVING
HOUSE_AND_KITCHEN: Audience = HOUSE | {KITCHEN}


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    roles: Audience


PRESETS = (
    Preset("house", "Hele huset", HOUSE),
    Preset("living", "Kun dem, der bor her nu", LIVING),
    Preset("former", "Kun tidligere beboere", FORMER),
    Preset("house_and_kitchen", "Hele huset og køkkenet", HOUSE_AND_KITCHEN),
)
BY_KEY = {preset.key: preset for preset in PRESETS}


def preset_of(audience: Iterable[str]) -> Preset | None:
    roles = frozenset(audience)
    return next((preset for preset in PRESETS if preset.roles == roles), None)


def label(audience: Iterable[str]) -> str:
    """How a page names an audience: its preset's name, or its roles'."""
    preset = preset_of(audience)
    if preset is not None:
        return preset.label
    return ", ".join(sorted(ROLE_LABELS.get(role, role) for role in audience))


def narrower_label(audience: Iterable[str]) -> str:
    """The label to show on a thing that is not for the whole house; "" for one that is."""
    return "" if frozenset(audience) == HOUSE else label(audience)


def includes(audience: Iterable[str], roles: Iterable[str]) -> bool:
    """Whether anyone holding `roles` is in `audience`."""
    return ADMINISTRATOR in roles or not frozenset(audience).isdisjoint(roles)


def visible(field: str, roles: Iterable[str]) -> Q:
    """Q for the rows whose audience (`field`) any of `roles` is in."""
    if ADMINISTRATOR in roles:
        return Q(pk__isnull=False)  # every row; a bare Q() would vanish when OR-ed with another
    return Q(**{f"{field}__overlap": sorted(roles)})


def members(audience: Iterable[str]) -> QuerySet[Account]:
    """The active accounts in `audience`, as a notification audience."""
    return holding({*audience, ADMINISTRATOR})


def everyone_in_the_house() -> list[str]:
    """The default audience of a field (a callable, so migrations can name it)."""
    return sorted(HOUSE)


def field(**kwargs: object) -> ArrayField:
    """The model field an audience is stored in. Defaults to the whole house."""
    return ArrayField(models.CharField(max_length=40), default=everyone_in_the_house, **kwargs)  # type: ignore[arg-type]
