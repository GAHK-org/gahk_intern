"""Why an account has the access it has, and what is about to change: what the access page shows.

Read from the same facts access.roles reads (the alumneliste's months and embedsgrupper, the grants,
the superuser flag), so the page cannot disagree with what the site does.
"""

import datetime
from dataclasses import dataclass

from django.utils.formats import date_format

from core.clock import current_date
from residents.models import WORKGROUP_ROLE, Residency, Resident, active_period

from .features import ALWAYS, Feature, Grade
from .models import Grant
from .permissions import permissions_of
from .policy import ADMINISTRATOR, POLICY, ROLE_LABELS, STAGES
from .roles import real_roles
from .stages import STAGE_LABELS, Stage, from_months

GRADE_LABELS = {
    Grade.NONE: "Ingen",
    Grade.VIEW: "Se",
    Grade.USE: "Bruge",
    Grade.MANAGE: "Styre",
    Grade.ADMINISTER: "Administrere",
}

FEATURE_LABELS = {
    Feature.FILES: "Filer",
    Feature.DASHBOARD: "Forsiden",
    Feature.DIRECTORY: "Alumneliste",
    Feature.PROFILES: "Profiler",
    Feature.MENTIONS: "@-nævn",
    Feature.STAMTREE: "Stamtræ",
    Feature.STATS: "Statistik",
    Feature.NOTIFICATIONS: "Notifikationer",
    Feature.DEN_HURTIGE: "Den Hurtige",
    Feature.OPSLAGSTAVLE: "Opslagstavlen",
    Feature.EVENTS: "Begivenheder",
    Feature.REPARATIONER: "Reparationer",
    Feature.PHOTO_ALBUM: "Fotoalbum",
    Feature.GAHKFLIX: "GAHKFlix",
    Feature.AK: "AK",
    Feature.OELKAELDER: "Ølkælder",
    Feature.VAERELSESTJEK: "Værelsestjek",
    Feature.SOEGVAERELSE: "Søg værelse",
    Feature.WIKI: "Wiki",
    Feature.ADMISSIONS: "Optagelse",
    Feature.WEBSITE: "Hjemmeside",
    Feature.ACCOUNTING: "Regnskab",
    Feature.SITE_ADMIN: "Site-admin",
    Feature.DJANGO_ADMIN: "Django admin",
    Feature.ACCESS: "Adgang",
}

# How far ahead the coming changes look.
HORIZON_MONTHS = 13


@dataclass(frozen=True)
class Reason:
    role: str
    label: str
    why: str


@dataclass(frozen=True)
class FeatureAccess:
    label: str
    grade: Grade
    grade_label: str
    given_by: str  # the roles that give this grade


@dataclass(frozen=True)
class Change:
    on: datetime.date
    text: str


@dataclass(frozen=True)
class Explanation:
    roles: list[Reason]
    features: list[FeatureAccess]
    changes: list[Change]


def _index(year: int, month: int) -> int:
    return year * 12 + month - 1


def _first_day(index: int) -> datetime.date:
    return datetime.date(index // 12, index % 12 + 1, 1)


def _month(index: int) -> str:
    return date_format(_first_day(index), "F Y")


def _day(day: datetime.date) -> str:
    return date_format(day, "j. F Y")


def _months(resident: Resident) -> dict[int, str]:
    """{month index: embedsgruppe name or ""} for every month the account is on a list."""
    return {
        _index(year, month): name or ""
        for year, month, name in resident.residencies.values_list("year", "month", "workgroup__name")
    }


def _stage_on(months: dict[int, str], now: int) -> Stage | None:
    past = [m for m in months if m <= now]
    return from_months(now, max(past) if past else None, max(months) if months else None)


def _stage_why(stage: Stage, months: dict[int, str], now: int) -> str:
    past = [m for m in months if m <= now]
    if stage == Stage.RESIDENT:
        return f"På alumnelisten for {_month(now)}."
    if stage == Stage.MOVING_IN:
        return f"På alumnelisten fra {_month(min(m for m in months if m > now))}."
    return f"Sidst på alumnelisten i {_month(max(past))}."


def _reasons(resident: Resident, roles: frozenset[str], months: dict[int, str], now: int) -> list[Reason]:
    reasons: list[Reason] = []
    stage = next((role for role in roles if role in STAGES), None)
    if stage is not None:
        own = _stage_on(months, now)
        if own is not None and own.value == stage:
            why = _stage_why(own, months, now)
        else:
            why = "Fordi du er administrator." if ADMINISTRATOR in roles else "Givet i hånden."
        reasons.append(Reason(stage, STAGE_LABELS[Stage(stage)], why))
    workgroup = months.get(now, "")
    listed = WORKGROUP_ROLE.get(workgroup)
    if listed and listed in roles:
        reasons.append(Reason(listed, ROLE_LABELS[listed], f"{workgroup} på alumnelisten for {_month(now)}."))
    for grant in Grant.objects.active().filter(resident=resident).select_related("granted_by"):
        until = f"til og med {_day(grant.ends_on)}" if grant.ends_on else "indtil videre"
        by = f" af {grant.granted_by.full_name}" if grant.granted_by else ""
        why = f"Givet{by} fra {_day(grant.starts_on)} {until}: {grant.reason}"
        reasons.append(Reason(grant.role, ROLE_LABELS.get(grant.role, grant.role), why))
    if resident.is_superuser:
        reasons.append(Reason(ADMINISTRATOR, ROLE_LABELS[ADMINISTRATOR], "Superbruger."))
    return reasons


def _features(roles: frozenset[str]) -> list[FeatureAccess]:
    permissions = permissions_of(roles)
    out = []
    for feature in Feature:
        if feature in ALWAYS:
            continue
        grade = permissions.grade(feature)
        givers = sorted(
            ROLE_LABELS.get(role, role)
            for role in roles
            if grade > Grade.NONE and permissions_of({role}).grade(feature) == grade and role in POLICY
        )
        out.append(FeatureAccess(FEATURE_LABELS[feature], grade, GRADE_LABELS[grade], ", ".join(givers)))
    return out


def _changes(resident: Resident, months: dict[int, str], now: int) -> list[Change]:
    """What will change, as far as is known: the lists made so far, and after the newest one only
    for someone not on it, whose moving out then runs its course."""
    today = current_date()
    changes: list[Change] = []
    newest = max(
        (_index(y, m) for y, m in Residency.objects.values_list("year", "month").distinct()), default=now
    )
    on_newest = bool(months) and max(months) >= newest
    stage, workgroup = _stage_on(months, now), months.get(now, "")
    for ahead in range(now + 1, now + HORIZON_MONTHS + 1):
        if ahead > newest and on_newest:
            break  # no list yet says what they do then
        on = _first_day(ahead)
        next_stage = _stage_on(months, ahead)
        if next_stage != stage:
            label = STAGE_LABELS[next_stage] if next_stage else "Ikke længere på nogen liste"
            changes.append(Change(on, f"{label}."))
            stage = next_stage
        next_workgroup = months.get(ahead, "")
        if ahead in months and next_workgroup != workgroup:
            role = WORKGROUP_ROLE.get(next_workgroup)
            label = ROLE_LABELS[role] if role else ""
            if not next_workgroup:
                text = "Ingen embedsgruppe på listen."
            elif label and label != next_workgroup:
                text = f"{next_workgroup} på listen, med rollen {label}."
            else:
                text = f"{next_workgroup} på listen."
            changes.append(Change(on, text))
            workgroup = next_workgroup
    for grant in Grant.objects.filter(resident=resident):
        label = ROLE_LABELS.get(grant.role, grant.role)
        if grant.starts_on > today:
            changes.append(Change(grant.starts_on, f"{label}, givet i hånden."))
        if grant.ends_on is not None and grant.ends_on >= today:
            changes.append(Change(grant.ends_on + datetime.timedelta(days=1), f"{label} ophører."))
    return sorted(changes, key=lambda change: change.on)


def explain(resident: Resident) -> Explanation:
    roles = real_roles(resident)
    months = _months(resident)
    now = _index(*active_period())
    return Explanation(
        roles=_reasons(resident, roles, months, now),
        features=_features(roles),
        changes=_changes(resident, months, now),
    )


def day(on: datetime.date) -> str:
    return _day(on)
