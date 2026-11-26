"""Where someone is in their life at the house, worked out from the alumneliste: one stage each.

The month in effect is the newest published list that has started (residents.models.active_period).

  * RESIDENT   on the list in effect;
  * MOVING_IN  on a coming list only (also someone moving back in, until their first month starts);
  * MOVED_OUT_* graded by how long ago they were last on a list in effect: the first month off the
    list is "under 1 month", months 2–3 "1–3 months", months 4–12 "3–12 months", after that "over a
    year";
  * no stage   never on any list (one of the kitchen staff, an account with no resident).

Months, not the move-out date: the list is kept every month and already decides who lives here
everywhere else. A stage is a role like any other (access.policy), so the sync (access.roles) writes
it into the account's groups: when the account's months change, and just after midnight, when a list
made ahead takes effect or a month has passed.
"""

from collections.abc import Iterable
from enum import StrEnum

from django.db.models import Case, F, IntegerField, Max, When

from residents.models import Residency, active_period


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


def month_index(year: int, month: int) -> int:
    return year * 12 + month - 1


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


def stages_of(account_ids: Iterable[int], now: int | None = None) -> dict[int, Stage]:
    """Each account's stage by its resident's months, in one query. Accounts with no stage (never on
    a list, or no resident at all) are left out. `now`: the month in effect's index."""
    now = month_index(*active_period()) if now is None else now
    months = (
        Residency.objects.filter(resident__person__account__in=list(account_ids))
        .annotate(index=_MONTH)
        .values("resident__person__account")
        .annotate(
            last=Max("index"),
            last_past=Max(Case(When(index__lte=now, then="index"), output_field=IntegerField())),
        )
        .values_list("resident__person__account", "last_past", "last")
    )
    out: dict[int, Stage] = {}
    for account_id, last_past, last in months:
        stage = from_months(now, last_past, last)
        if stage is not None:
            out[account_id] = stage
    return out
