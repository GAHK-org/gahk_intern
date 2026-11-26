"""THE TABLE: what each role gives. The whole rule of who may do what is here.

Someone holds a set of roles (access.roles says which), and their access to a feature is the highest
grade any of their roles gives it; their extras are every role's together. Nothing takes away what
another role gives.

Code never asks for a role. It asks for a permission (`can(request, "oelkaelder.manage")`), and this
table says which roles have it, so "who may manage Reparationer" is answered here rather than by a
search through the apps.
"""

from collections.abc import Iterable, Mapping

from residents.models import Role

from .features import ALWAYS, EXTRAS, GRADE_ACTIONS, Feature, Grade

# Roles that are no embedsgruppe. A group's role is its residents.models.Role value.
RESIDENT = "resident"  # lives here: every account but the kitchen's
KITCHEN = "kitchen"  # the kitchen staff
ADMINISTRATOR = Role.ADMINISTRATOR.value  # Netværksgruppen: everything (also superusers)

ROLE_LABELS: dict[str, str] = {
    RESIDENT: "Beboer",
    KITCHEN: "Køkkenet",
    **{value: str(label) for value, label in Role.choices},
}

# A role's entry: each feature's grade, or the name of an extra action of that feature.
Entry = Mapping[Feature, Grade | str]

# What a resident uses: every feature but the group tools.
_GROUP_TOOLS = frozenset(
    {
        Feature.ADMISSIONS,
        Feature.WEBSITE,
        Feature.ACCOUNTING,
        Feature.SITE_ADMIN,
        Feature.DJANGO_ADMIN,
    }
)
_RESIDENT_FEATURES = frozenset(Feature) - ALWAYS - _GROUP_TOOLS


def _all(features: Iterable[Feature], grade: Grade) -> dict[Feature, Grade | str]:
    return dict.fromkeys(features, grade)


POLICY: dict[str, Entry] = {
    RESIDENT: {**_all(_RESIDENT_FEATURES, Grade.USE)},
    # Den Hurtige's Køkkenet channel and their own opslag only: the features narrow it further inside.
    KITCHEN: _all(
        {
            Feature.FILES,
            Feature.DEN_HURTIGE,
            Feature.OPSLAGSTAVLE,
            Feature.REPARATIONER,
            Feature.NOTIFICATIONS,
        },
        Grade.USE,
    ),
    Role.INDSTILLING: {
        Feature.ADMISSIONS: Grade.MANAGE,
        Feature.DIRECTORY: Grade.MANAGE,
        Feature.SOEGVAERELSE: Grade.MANAGE,
        Feature.WEBSITE: Grade.MANAGE,
    },
    Role.INSPEKTION: {
        Feature.DEN_HURTIGE: Grade.MANAGE,
        Feature.OPSLAGSTAVLE: Grade.MANAGE,
        Feature.REPARATIONER: Grade.MANAGE,
        Feature.WEBSITE: Grade.MANAGE,
    },
    Role.KOKKENGRUPPE: {},  # no tool of its own yet
    Role.AK: {Feature.AK: Grade.MANAGE, Feature.VAERELSESTJEK: Grade.MANAGE},
    Role.OELKAELDER: {Feature.OELKAELDER: Grade.MANAGE},
    Role.REGNSKAB: {Feature.ACCOUNTING: Grade.MANAGE},
    Role.PR: {Feature.WEBSITE: Grade.MANAGE},
    Role.REPPER: {Feature.REPARATIONER: Grade.MANAGE},
    Role.VICEVAERT: {Feature.REPARATIONER: "move"},
    Role.FOTO: {Feature.PHOTO_ALBUM: Grade.MANAGE, Feature.GAHKFLIX: Grade.MANAGE},
    ADMINISTRATOR: _all(Feature, Grade.ADMINISTER),
}
for _entry in POLICY.values():
    for _feature in ALWAYS:
        _entry.setdefault(_feature, Grade.USE)  # type: ignore[attr-defined]


def roles_with(feature: Feature, action: str) -> frozenset[str]:
    """The roles whose entry gives `feature.action` by itself."""
    return frozenset(role for role, entry in POLICY.items() if _gives(entry, feature, action))


def _gives(entry: Entry, feature: Feature, action: str) -> bool:
    value = entry.get(feature, Grade.NONE)
    if isinstance(value, str):
        return value == action
    if action in EXTRAS.get(feature, frozenset()):
        return value >= Grade.MANAGE
    return value >= GRADE_ACTIONS[action]
