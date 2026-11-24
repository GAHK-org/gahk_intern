"""Roles stop being stored per month: an embedsgruppe's role is read from the month's list, and every
other role becomes a grant with a first and a last day (access.models.Grant).

Forwards: each run of consecutive months in which a resident held a role their embedsgruppe on that
month's list did not give them becomes one grant, from the first day of the run's first month
through the last day of its last month — or until ended, when the run reaches the newest list.
Roles the list gave are dropped: they are read from the list now.

Backwards: a row per month again, for every month on any list, from the embedsgrupper on the lists
and from the grants that held in the month.
"""

import datetime

from django.db import migrations

# residents.models.WORKGROUP_ROLE as it was when this was written.
WORKGROUP_ROLE = {
    "Indstillingen": "indstilling",
    "Inspektionen": "inspektion",
    "Køkkengruppen": "kokkengruppe",
    "AK-gruppen": "ak",
    "Ølkælderen": "oelkaelder",
    "Regnskabsgruppen": "regnskab",
    "PR-gruppen": "pr",
    "Repperne": "repper",
    "Vicevært": "vicevaert",
    "Fotogruppen": "foto",
}
REASON = "Fra rollesiden (før roller fik datoer)"


def _index(year: int, month: int) -> int:
    return year * 12 + month - 1


def _first_day(index: int) -> datetime.date:
    return datetime.date(index // 12, index % 12 + 1, 1)


def _last_day(index: int) -> datetime.date:
    return _first_day(index + 1) - datetime.timedelta(days=1)


def forwards(apps, schema_editor):  # noqa: ANN001, ANN201
    Residency = apps.get_model("residents", "Residency")
    RoleAssignment = apps.get_model("residents", "RoleAssignment")
    Grant = apps.get_model("access", "Grant")
    listed = {
        (r["resident_id"], r["year"], r["month"]): WORKGROUP_ROLE.get(r["workgroup__name"] or "")
        for r in Residency.objects.values("resident_id", "year", "month", "workgroup__name")
    }
    newest = max((_index(y, m) for (_r, y, m) in listed), default=None)
    held: dict[tuple[int, str], list[int]] = {}
    for a in RoleAssignment.objects.values("resident_id", "role", "year", "month"):
        if listed.get((a["resident_id"], a["year"], a["month"])) == a["role"]:
            continue  # the list gives it
        held.setdefault((a["resident_id"], a["role"]), []).append(_index(a["year"], a["month"]))
    for (resident_id, role), months in held.items():
        months.sort()
        runs: list[list[int]] = [[months[0]]]
        for month in months[1:]:
            if month == runs[-1][-1] + 1:
                runs[-1].append(month)
            elif month != runs[-1][-1]:
                runs.append([month])
        for run in runs:
            open_ended = newest is not None and run[-1] >= newest
            Grant.objects.create(
                resident_id=resident_id,
                role=role,
                reason=REASON,
                starts_on=_first_day(run[0]),
                ends_on=None if open_ended else _last_day(run[-1]),
            )


def backwards(apps, schema_editor):  # noqa: ANN001, ANN201
    Residency = apps.get_model("residents", "Residency")
    RoleAssignment = apps.get_model("residents", "RoleAssignment")
    Grant = apps.get_model("access", "Grant")
    months = sorted(set(Residency.objects.values_list("year", "month")))
    for r in Residency.objects.filter(workgroup__isnull=False).values(
        "resident_id", "year", "month", "workgroup__name"
    ):
        role = WORKGROUP_ROLE.get(r["workgroup__name"])
        if role:
            RoleAssignment.objects.get_or_create(
                resident_id=r["resident_id"], role=role, year=r["year"], month=r["month"]
            )
    roles = {
        "indstilling",
        "inspektion",
        "kokkengruppe",
        "ak",
        "oelkaelder",
        "regnskab",
        "pr",
        "repper",
        "vicevaert",
        "foto",
        "administrator",
    }
    for grant in Grant.objects.filter(role__in=roles):
        for year, month in months:
            first, last = _first_day(_index(year, month)), _last_day(_index(year, month))
            if grant.starts_on <= last and (grant.ends_on is None or grant.ends_on >= first):
                RoleAssignment.objects.get_or_create(
                    resident_id=grant.resident_id, role=grant.role, year=year, month=month
                )
    Grant.objects.filter(reason=REASON).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("residents", "0013_remove_resident_is_kitchen"),
        ("access", "0002_shared_kitchen_login"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
