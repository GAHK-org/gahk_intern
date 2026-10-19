"""Who gets notified about what on Reparationer — the policy half of its notifications (the
notifications app is the machinery, shared with every other feature). All three go out under one
category, "Reparationer", on the settings page.

Three events, two audiences, matching the two-crew handoff in models.RepairTask:

  * a new repair is reported -> every Vicevært, because `responsible` starts life as Vicevært (they
    triage everything that comes in);
  * a repair is handed to Reppergruppen -> every Repper — the ticket is now theirs to actually plan
    and fix;
  * a repair is handed back to Viceværterne -> every Vicevært, for the same reason in the other
    direction (see views.set_responsible: a handoff goes both ways).

Only the crew INHERITING a ticket is told. The crew letting it go contains whoever just pressed the
button, and paging a group about their own action is how people learn to ignore notifications.

Both audiences are narrowed to the role's CURRENT holders (this month's RoleAssignment): someone who
was on Reppergruppen last year and rotated off should not keep getting paged.
"""

from typing import TYPE_CHECKING

from django.db.models import Q, QuerySet

from notifications import api as notifications
from residents.api import Resident, Role, resident_ids_with_roles

if TYPE_CHECKING:
    from .models import RepairTask

BOARD_URL = "/intern/reparationer/"


def task_url(task: "RepairTask") -> str:
    return f"{BOARD_URL}{task.pk}"


def _residents_with_role(role: str) -> QuerySet[Resident]:
    """Residents currently holding `role` for the active period. administrator counts too — it
    implies every role (residents.permissions.real_roles), and a superuser may hold the role with no
    RoleAssignment row at all."""
    resident_ids = resident_ids_with_roles([role, Role.ADMINISTRATOR])
    return Resident.objects.filter(is_active=True).filter(Q(pk__in=resident_ids) | Q(is_superuser=True))


def notify_new_task(task: "RepairTask") -> None:
    """A new repair lands on Viceværterne's desk by default — tell them, except the person who just
    reported it."""
    notifications.notify(
        _residents_with_role(Role.VICEVAERT),
        category=notifications.REPAIRS,
        title="Ny reparation meldt ind",
        body=task.title,
        url=task_url(task),
        exclude=task.reported_by_id,
    )


def notify_handed_to_repper(task: "RepairTask") -> None:
    """The repair was handed to Reppergruppen — tell them it is now theirs."""
    notifications.notify(
        _residents_with_role(Role.REPPER),
        category=notifications.REPAIRS,
        title="Reparation overdraget til Repper",
        body=task.title,
        url=task_url(task),
    )


def notify_handed_to_vicevaert(task: "RepairTask") -> None:
    """The repair went back to Viceværterne — the mirror of notify_handed_to_repper."""
    notifications.notify(
        _residents_with_role(Role.VICEVAERT),
        category=notifications.REPAIRS,
        title="Reparation sendt tilbage til Viceværterne",
        body=task.title,
        url=task_url(task),
    )
