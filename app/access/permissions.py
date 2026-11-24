"""What a set of roles may do, worked out from the table once and then asked many times."""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache

from .features import EXTRAS, GRADE_ACTIONS, HOME_FEATURES, Feature, Grade, parse
from .policy import POLICY

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Permissions:
    grades: dict[Feature, Grade]
    extras: frozenset[str]  # "feature.action"

    def grade(self, feature: Feature | None) -> Grade:
        return Grade.NONE if feature is None else self.grades.get(feature, Grade.NONE)

    def allows(self, permission: str) -> bool:
        """Whether these roles have `permission` ("oelkaelder.manage", "reparationer.move")."""
        feature, action = parse(permission)
        grade = self.grade(feature)
        if action in GRADE_ACTIONS:
            return grade >= GRADE_ACTIONS[action]
        return grade >= Grade.MANAGE or permission in self.extras

    def allows_request(self, feature: Feature | None, method: str) -> bool:
        """The gate's question: view for a request that changes nothing, use for one that may."""
        needed = Grade.VIEW if method in SAFE_METHODS else Grade.USE
        return self.grade(feature) >= needed

    def opens(self, feature: Feature | None) -> bool:
        """Whether a link to a page of `feature` leads somewhere for these roles. The front page does
        for anyone with a home to be sent on to; a link that is no page of ours refuses nobody."""
        if feature is None:
            return True
        if feature == Feature.DASHBOARD:
            return any(self.grade(f) >= Grade.VIEW for f in (Feature.DASHBOARD, *HOME_FEATURES))
        return self.grade(feature) >= Grade.VIEW

    def features(self, grade: Grade = Grade.VIEW) -> frozenset[Feature]:
        return frozenset(f for f, g in self.grades.items() if g >= grade)


@cache
def _of(roles: frozenset[str]) -> Permissions:
    grades: dict[Feature, Grade] = {}
    extras: set[str] = set()
    for role in roles:
        for feature, value in POLICY.get(role, {}).items():
            if isinstance(value, str):
                extras.add(f"{feature}.{value}")
                grades[feature] = max(grades.get(feature, Grade.NONE), Grade.USE)
            elif value > grades.get(feature, Grade.NONE):
                grades[feature] = value
    for feature, names in EXTRAS.items():
        if grades.get(feature, Grade.NONE) >= Grade.MANAGE:
            extras.update(f"{feature}.{name}" for name in names)
    return Permissions(grades, frozenset(extras))


def permissions_of(roles: Iterable[str]) -> Permissions:
    """The permissions a set of roles gives. Cached: there are few distinct sets."""
    return _of(frozenset(roles))
