"""Who gets notified about what on Reparationer — the policy half of push (core.push is transport,
shared with Den Hurtige and Opslagstavlen).

Two events, two audiences, matching the two-crew handoff in models.RepairTask:

  * a new repair is reported -> every Vicevært who has opted in, because `responsible` starts life
    as Vicevært (they triage everything that comes in);
  * a repair is handed to Reppergruppen -> every Repper who has opted in — the ticket is now theirs
    to actually plan and fix;
  * a repair is handed back to Viceværterne -> every Vicevært who has opted in, for the same reason
    in the other direction (see views.set_responsible: a handoff goes both ways).

Only the crew INHERITING a ticket is told. The crew letting it go contains whoever just pressed the
button, and paging a group about their own action is how people learn to ignore notifications.

Both audiences are narrowed to the role's CURRENT holders (this month's RoleAssignment), not to
"everyone who ever subscribed": someone who opted in while on Reppergruppen last year and rotated
off should not keep getting paged.
"""

from typing import TYPE_CHECKING

from django.db.models import Q, QuerySet

from core import push
from residents.models import Role, RoleAssignment, active_period

if TYPE_CHECKING:
    from core.models import PushSubscription
    from residents.models import Resident

    from .models import RepairTask

TOPIC = "reparationer"
BOARD_URL = "/intern/reparationer/"


def task_url(task: "RepairTask") -> str:
    return f"{BOARD_URL}{task.pk}"


def is_subscribed(resident: "Resident") -> bool:
    """Whether any of this resident's devices wants Reparationer notifications — the initial state
    of the subscribe toggle, mirroring opslagstavle.services.is_subscribed."""
    return push.subscribers(TOPIC).filter(user=resident).exists()


def _subscribers_with_role(role: str) -> "QuerySet[PushSubscription]":
    """Devices opted in to the Reparationer topic, owned by someone currently holding `role` for the
    active period. administrator counts too — it implies every role (residents.permissions.real_roles),
    and a superuser may hold the role with no RoleAssignment row at all."""
    year, month = active_period()
    resident_ids = RoleAssignment.objects.filter(
        year=year, month=month, role__in=[role, Role.ADMINISTRATOR]
    ).values_list("resident_id", flat=True)
    return push.subscribers(TOPIC).filter(Q(user_id__in=resident_ids) | Q(user__is_superuser=True))


def notify_new_task(task: "RepairTask") -> None:
    """A new repair lands on Viceværterne's desk by default — tell them, except the person who just
    reported it (mirrors opslagstavle.services.notify_new_notice excluding the author)."""
    push.send(
        _subscribers_with_role(Role.VICEVAERT).exclude(user_id=task.reported_by_id),
        head="Ny reparation meldt ind",
        body=task.title,
        url=task_url(task),
    )


def notify_handed_to_repper(task: "RepairTask") -> None:
    """The repair was handed to Reppergruppen — tell them it is now theirs."""
    push.send(
        _subscribers_with_role(Role.REPPER),
        head="Reparation overdraget til Repper",
        body=task.title,
        url=task_url(task),
    )


def notify_handed_to_vicevaert(task: "RepairTask") -> None:
    """The repair went back to Viceværterne — the mirror of notify_handed_to_repper."""
    push.send(
        _subscribers_with_role(Role.VICEVAERT),
        head="Reparation sendt tilbage til Viceværterne",
        body=task.title,
        url=task_url(task),
    )
