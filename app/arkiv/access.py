"""Who may reach Arkiv, and which folders they may see.

    TO OPEN IT TO EVERY RESIDENT: set ACCESS_ROLES = None.

The gate MECHANISM is core.rollout, as in den_hurtige, opslagstavle and events. What is here is this
feature's own policy, and it has the same shape as begivenheder's: a folder can be invisible to a
resident who is otherwise allowed into the whole feature.

`visible_to` IS THE CHOKEPOINT. Every view, every listing, every download and anything added later
must start from it. A folder you may not see has to be *absent*, not forbidden: the detail view 404s
rather than 403s, because a 403 confirms that a folder with that id exists, which is precisely what
Regnskabsgruppen's folder is hiding.

    404  you may not know it exists          (a group folder you are not in)
    403  you know it exists, but not this    (a folder you can read but not write to)

MEMBERSHIP IS CURRENT, NOT HISTORICAL, and that was decided rather than defaulted. Access resolves
through `Residency` for `active_period()` - the same monthly list that governs everything else - so
leaving Regnskabsgruppen in the maanedsliste ends access to its documents that month. The cost is
real and worth stating: a resident loses the folders of a group they were in last year, including
photographs they took themselves. The answer to that is not to widen the rule but to file such
things in a folder with no `workgroup`, which every resident can read. A shared archive that only
its current caretakers can see is a filing mistake, not an access-control one.

NO ROLE SEES EVERYTHING. Administrators and Inspektionen manage the root folders (see
`can_manage_roots`) but get no special *read* access, deliberately and for the same reason
events/access.py refuses it: a group folder's whole promise is that non-members cannot read it, and
"except Inspektionen" makes that promise false in exactly the case anyone would care about. The
escape hatch for a genuinely misfiled document is the Django admin, which has always seen every
table.
"""

from collections.abc import Collection

from django.db.models import Q, QuerySet
from django.http import HttpRequest

from core.rollout import Gate
from residents.models import Residency, Resident, Role, active_period
from residents.permissions import View, current_resident, request_has_role

from .models import ArchiveFile, ArchiveFolder

# None = every logged-in resident. A tuple = only those roles (administrator implies every role, so
# administrators and superusers are always in).
#
# Gated for a first pass to Inspektionen and Netvaerksgruppen, matching what opslagstavlen and
# begivenheder did. The archive is the one feature where a premature opening is hard to walk back:
# residents will start filing things, and the folder tree they build in the first month is the one
# the kollegium lives with. Open it once the root structure is agreed, not before.
#
# TO OPEN IT TO EVERY RESIDENT: set ACCESS_ROLES = None. That one edit widens every view, the
# sidebar entry and the "Under test" chip together.
ACCESS_ROLES: tuple[str, ...] | None = (Role.ADMINISTRATOR, Role.INSPEKTION)

# Read through a lambda, never passed by value: this global is what tests rebind and what the edit
# above would flip, and a Gate holding the value would freeze at import. See core.rollout.
_GATE = Gate(lambda: ACCESS_ROLES)

is_limited = _GATE.is_limited


def roles_allowed(roles: Collection[str]) -> bool:
    """Whether a role set may use Arkiv. Takes roles rather than a request so the sidebar, which only
    has the effective role set to hand, can ask exactly the same question as the views."""
    return _GATE.roles_allowed(roles)


def request_allowed(request: HttpRequest) -> bool:
    return _GATE.request_allowed(request)


def access_required(view: View) -> View:
    """@login_required plus the rollout gate. Every view gets it, including htmx partials: a partial
    that answers 200 to someone the page 403s hands the feature out through the back."""
    return _GATE.required(view)


def current_workgroup_ids(resident: Resident) -> list[int]:
    """The workgroup(s) `resident` belongs to in the active period.

    A list, though the schema allows at most one: `Residency` is unique on (resident, year, month),
    so this returns zero or one id. Kept as a list because every caller wants `__in`, and because a
    resident with no residency this month - an alumnus, or someone between lists - must produce an
    empty set rather than None, which would silently match nothing in some places and everything in
    others.
    """
    year, month = active_period()
    return list(
        Residency.objects.filter(resident=resident, year=year, month=month, workgroup__isnull=False)
        .values_list("workgroup_id", flat=True)
        .distinct()
    )


def visible_folders(resident: Resident) -> QuerySet[ArchiveFolder]:
    """The ONLY queryset any view, listing or download may start from.

    A folder is visible when it is not soft-deleted AND either it belongs to no embedsgruppe
    (`effective_workgroup IS NULL`, the shared archive) or the resident is in that embedsgruppe this
    month.

    Filtered on the DENORMALISED `effective_workgroup`, which is what makes this one indexed
    predicate instead of an ancestor walk - and what makes it impossible to reach a private
    subfolder by guessing its id just because its parent was public.
    """
    return ArchiveFolder.objects.alive().filter(
        Q(effective_workgroup__isnull=True) | Q(effective_workgroup_id__in=current_workgroup_ids(resident))
    )


def visible_files(resident: Resident) -> QuerySet[ArchiveFile]:
    """Files inherit their folder's visibility, and nothing else grants it.

    `folder_id__in=<subquery>` rather than a join across the FK: a join would let a later
    `annotate(Count(...))` multiply rows before the aggregate reached them, which is the trap
    events/access.py documents at length.
    """
    return ArchiveFile.objects.alive().filter(folder_id__in=visible_folders(resident).values("pk"))


def can_read(folder: ArchiveFolder, resident: Resident) -> bool:
    """Whether `resident` may see `folder` at all. Asked per object; `visible_folders` is what
    queries use."""
    if folder.deleted_at is not None:
        return False
    if folder.effective_workgroup_id is None:
        return True
    return folder.effective_workgroup_id in current_workgroup_ids(resident)


def can_manage_roots(request: HttpRequest) -> bool:
    """Who shapes the top level.

    Root folders are the kollegium's filing system - one per embedsgruppe, plus the shared areas -
    so they are Inspektionen's and Netvaerksgruppen's to arrange. Everything *below* a root is made
    by whoever can already see it, because needing a ticket to make a folder for this year's fest is
    how an archive turns back into a chat thread full of attachments.
    """
    return request_has_role(request, Role.ADMINISTRATOR, Role.INSPEKTION)


def can_write(folder: ArchiveFolder, request: HttpRequest) -> bool:
    """Whether `request` may add a subfolder or a file to `folder`.

    Reading is the permission; writing follows it. There is deliberately no separate read-only tier:
    the kollegium is a hundred people who already trust each other with a shared Dropbox, and every
    write is attributed and soft-deleted rather than lost.
    """
    resident = current_resident(request)
    return resident is not None and can_read(folder, resident)


def can_delete_file(file: ArchiveFile, request: HttpRequest) -> bool:
    """Whether `request` may remove `file`.

    ANYONE WHO CAN WRITE TO THE FOLDER, not only the uploader, and that is deliberate. The thing
    being replaced is a shared Dropbox password where everyone could already delete everything; an
    archive where only the original uploader can tidy up accumulates duplicates and mistakes nobody
    is allowed to fix, and half the uploaders have moved out.

    What makes that safe is that it is not a delete. The row is marked, the bytes stay, and
    `deleted_by` records who did it - so the failure mode is "ask them to put it back", not "it is
    gone". A hard delete would need a different answer.
    """
    return can_write(file.folder, request)
