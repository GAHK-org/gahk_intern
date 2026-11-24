"""Where someone is in their life at the house, worked out from the alumneliste: one stage each.

The month in effect is the newest published list that has started (residents.models.active_period).

  * RESIDENT   on the list in effect;
  * MOVING_IN  on a coming list only (also someone moving back in, until their first month starts);
  * MOVED_OUT_* graded by how long ago they were last on a list in effect: the first month off the
    list is "under 1 month", months 2–3 "1–3 months", months 4–12 "3–12 months", after that "over a
    year";
  * no stage   never on any list.

Months, not the move-out date: the list is kept every month and already decides who lives here
everywhere else. Worked out when asked (one query for an account, one annotation for a queryset),
never stored, so nothing has to keep it current: a list made ahead takes effect on the 1st and the
months pass on their own.
"""

from enum import StrEnum

from django.db.models import Case, F, IntegerField, OuterRef, QuerySet, Subquery, Value, When

from residents.models import Residency, Resident, active_period


class Stage(StrEnum):
    MOVING_IN = "moving_in"
    RESIDENT = "resident"
    MOVED_OUT_UNDER_1_MONTH = "moved_out_under_1_month"
    MOVED_OUT_1_TO_3_MONTHS = "moved_out_1_to_3_months"
    MOVED_OUT_3_TO_12_MONTHS = "moved_out_3_to_12_months"
    MOVED_OUT_OVER_A_YEAR = "moved_out_over_a_year"


STAGE_LABELS = {
    Stage.MOVING_IN: "Flytter ind",
    Stage.RESIDENT: "Beboer",
    Stage.MOVED_OUT_UNDER_1_MONTH: "Fraflyttet for under 1 måned siden",
    Stage.MOVED_OUT_1_TO_3_MONTHS: "Fraflyttet for 1–3 måneder siden",
    Stage.MOVED_OUT_3_TO_12_MONTHS: "Fraflyttet for 3–12 måneder siden",
    Stage.MOVED_OUT_OVER_A_YEAR: "Fraflyttet for over et år siden",
}

# The stage of an account that has never been on a list and holds no grant (access.roles). None in
# production: it has no access at all. The test suite makes it RESIDENT (tests/conftest.py), so a
# test's account needs no months on a list to use the site.
NEVER_LISTED: Stage | None = None


def _index(year: int, month: int) -> int:
    return year * 12 + month - 1


def now_index() -> int:
    return _index(*active_period())


def from_months(now: int, last_past: int | None, last: int | None) -> Stage | None:
    """From month indexes: the month in effect, the last month on a list in effect by then, and the
    last month on any list (a coming one included)."""
    if last_past == now:
        return Stage.RESIDENT
    if last is not None and last > now:
        return Stage.MOVING_IN
    if last_past is None:
        return None
    away = now - last_past  # 1 in the first month off the list
    if away <= 1:
        return Stage.MOVED_OUT_UNDER_1_MONTH
    if away <= 3:
        return Stage.MOVED_OUT_1_TO_3_MONTHS
    if away <= 12:
        return Stage.MOVED_OUT_3_TO_12_MONTHS
    return Stage.MOVED_OUT_OVER_A_YEAR


_MONTH = F("year") * 12 + F("month") - 1


def stage_of(resident: Resident) -> Stage | None:
    """One account's stage by its months alone (access.roles adds what groups and grants do)."""
    annotated = with_stage(Resident.objects.filter(pk=resident.pk))
    value = annotated.values_list("access_stage", flat=True).first()  # type: ignore[misc]  # annotated
    return Stage(value) if value else None


def with_stage(accounts: QuerySet[Resident], now: int | None = None) -> QuerySet[Resident]:
    """`accounts` annotated with `access_stage`: the stage's value, or None when never on a list.
    For filtering in SQL; the same rule as from_months. `now`: the month in effect's index."""
    now = now_index() if now is None else now
    months = Residency.objects.filter(resident_id=OuterRef("pk")).annotate(month_index=_MONTH)
    last_past = months.filter(month_index__lte=now).order_by("-month_index").values("month_index")[:1]
    last = months.order_by("-month_index").values("month_index")[:1]
    annotated = accounts.annotate(
        _last_past=Subquery(last_past, output_field=IntegerField()),
        _last=Subquery(last, output_field=IntegerField()),
    )
    return annotated.annotate(
        access_stage=Case(
            When(_last_past=now, then=Value(Stage.RESIDENT.value)),
            When(_last__gt=now, then=Value(Stage.MOVING_IN.value)),
            When(_last_past__isnull=True, then=Value(None)),
            When(_last_past__gte=now - 1, then=Value(Stage.MOVED_OUT_UNDER_1_MONTH.value)),
            When(_last_past__gte=now - 3, then=Value(Stage.MOVED_OUT_1_TO_3_MONTHS.value)),
            When(_last_past__gte=now - 12, then=Value(Stage.MOVED_OUT_3_TO_12_MONTHS.value)),
            default=Value(Stage.MOVED_OUT_OVER_A_YEAR.value),
        )
    )
