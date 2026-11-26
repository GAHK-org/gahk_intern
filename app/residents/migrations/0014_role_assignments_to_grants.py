"""Roles stop being stored per month: an embedsgruppe's role is read from the month's list, and every
other role becomes a grant with a first and a last day (access.models.Grant).

Forwards: each run of consecutive months in which an account held a role its embedsgruppe on that
month's list did not give it becomes one grant, from the first day of the run's first month through
the last day of its last month, or until ended when the run reaches the newest list. Roles the list
gave are dropped: they are read from the list now. `is_staff` (entry to Django admin) then follows
administrator grants alone, as residents.signals keeps it from here on.

Backwards: a row per month again, for every month on any list, from the embedsgrupper on the lists
and from the grants that held in the month; `is_staff` for anyone with a row, as before.
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
ROLES = {*WORKGROUP_ROLE.values(), "administrator"}
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
        (r["resident__person__account"], r["year"], r["month"]): WORKGROUP_ROLE.get(
            r["workgroup__name"] or ""
        )
        for r in Residency.objects.filter(resident__person__account__isnull=False).values(
            "resident__person__account", "year", "month", "workgroup__name"
        )
    }
    months = Residency.objects.values_list("year", "month")
    newest = max((_index(y, m) for y, m in months), default=None)
    held: dict[tuple[int, str], list[int]] = {}
    for a in RoleAssignment.objects.values("resident_id", "role", "year", "month"):
        if listed.get((a["resident_id"], a["year"], a["month"])) == a["role"]:
            continue  # the list gives it
        held.setdefault((a["resident_id"], a["role"]), []).append(_index(a["year"], a["month"]))
    grants = []
    for (account_id, role), indexes in held.items():
        indexes.sort()
        runs: list[list[int]] = [[indexes[0]]]
        for index in indexes[1:]:
            if index == runs[-1][-1] + 1:
                runs[-1].append(index)
            elif index != runs[-1][-1]:
                runs.append([index])
        for run in runs:
            open_ended = newest is not None and run[-1] >= newest
            grants.append(
                Grant(
                    account_id=account_id,
                    role=role,
                    reason=REASON,
                    starts_on=_first_day(run[0]),
                    ends_on=None if open_ended else _last_day(run[-1]),
                )
            )
    Grant.objects.bulk_create(grants)
    Account = apps.get_model("residents", "Account")
    admins = Grant.objects.filter(role="administrator").values("account_id")
    Account.objects.filter(is_superuser=False).exclude(pk__in=admins).update(is_staff=False)
    Account.objects.filter(is_superuser=False, pk__in=admins).update(is_staff=True)


def backwards(apps, schema_editor):  # noqa: ANN001, ANN201
    Residency = apps.get_model("residents", "Residency")
    RoleAssignment = apps.get_model("residents", "RoleAssignment")
    Grant = apps.get_model("access", "Grant")
    rows = set()
    for r in Residency.objects.filter(
        workgroup__isnull=False, resident__person__account__isnull=False
    ).values("resident__person__account", "year", "month", "workgroup__name"):
        role = WORKGROUP_ROLE.get(r["workgroup__name"])
        if role:
            rows.add((r["resident__person__account"], role, r["year"], r["month"]))
    months = sorted(set(Residency.objects.values_list("year", "month")))
    for grant in Grant.objects.filter(role__in=ROLES):
        for year, month in months:
            first, last = _first_day(_index(year, month)), _last_day(_index(year, month))
            if grant.starts_on <= last and (grant.ends_on is None or grant.ends_on >= first):
                rows.add((grant.account_id, grant.role, year, month))
    RoleAssignment.objects.bulk_create(
        [RoleAssignment(resident_id=a, role=role, year=y, month=m) for a, role, y, m in sorted(rows)],
        ignore_conflicts=True,
    )
    Grant.objects.filter(reason=REASON).delete()
    Account = apps.get_model("residents", "Account")
    Account.objects.filter(pk__in=RoleAssignment.objects.values("resident_id")).update(is_staff=True)


class Migration(migrations.Migration):
    dependencies = [
        ("residents", "0013_people_and_residents"),
        ("access", "0002_grant"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
