"""Who may reach Ankebogen, and who may do what once they are there.

The staged rollout is over: ACCESS_ROLES is None and every resident is in.

    TO RE-GATE IT: set ACCESS_ROLES to a tuple of roles.

That one edit narrows every view, the sidebar entry and the notification audience together.

This module used to argue *against* having a staged-rollout gate at all, on the grounds that the
feature replaces a Facebook group everyone is already in, so a board only Inspektionen can see has
no content and cannot be meaningfully trialled. That reasoning was right about the board as a
*social space*, which is why the gate was always meant to come off quickly — and why it now has.
It was never the thing the trial tested, though: that was narrower and three people could answer
it, being whether posting, Markdown, image upload, reactions, comments and push work at all.
Finding that out with three was cheaper than finding it out with a hundred.

The gate itself now lives in core.rollout — this module keeps only ACCESS_ROLES and the per-object
policy below. It used to be a deliberate copy of den_hurtige/access.py's, on the grounds that both
were meant to be deleted and a shared core/rollout.py would outlive the reason it existed; that
comment set the condition for changing its mind ("if a third feature ever wants one, that is the
point to extract it"), and begivenheder was the third. The two copies had also already drifted in
wording, which is the first symptom of the thing the extraction prevents.

Every check below reads *effective* roles, so an administrator using the preview tool to view the
site as a beboer is locked out too — which is exactly the rollout being simulated. That real/
effective split is the security boundary in this codebase (see residents.permissions).
"""

from collections.abc import Collection

from django.db.models import QuerySet
from django.http import HttpRequest

from core.rollout import Gate
from residents.permissions import MODERATION_ROLES, View, request_has_role

from .models import Notice, NoticeComment

# None = every logged-in resident. A tuple = only those roles (administrator implies every role, so
# administrators and superusers are always in).
#
# Open to the whole kollegium. Two things follow on their own rather than needing an edit, and
# both are worth expecting instead of debugging: the "Under test" chip disappears (board.html reads
# is_limited()), and the notification audience widens, because services._audience already runs every
# fan-out through allowed_subscribers — so a new opslag now reaches every resident who has opted in
# to the topic rather than only the trial group's devices.
ACCESS_ROLES: tuple[str, ...] | None = None

# Read through a lambda, never passed by value: this module global is what tests rebind and what a
# future edit flips to None, and a Gate holding the value would freeze at import. See core.rollout.
_GATE = Gate(lambda: ACCESS_ROLES)

# The gate's surface, re-exported under the names this app has always used so no caller changed when
# the mechanism moved to core.
is_limited = _GATE.is_limited


def roles_allowed(roles: Collection[str]) -> bool:
    """Whether a role set may use the board."""
    return _GATE.roles_allowed(roles)


def request_allowed(request: HttpRequest) -> bool:
    """Same question for a request."""
    return _GATE.request_allowed(request)


def access_required(view: View) -> View:
    """@login_required plus the rollout gate. Every view gets this, not just the page."""
    return _GATE.required(view)


def allowed_subscribers(qs: QuerySet) -> QuerySet:
    """Narrow a push audience to devices whose owner can actually open the board."""
    return _GATE.allowed_subscribers(qs)


def can_moderate(request: HttpRequest) -> bool:
    """Whether this request may delete other people's content and pin/unpin.

    Inspektionen keep the kollegium's house rules, so they moderate the board the same way they do
    everything else; administrator is included because it implies every role.
    """
    return request.user.is_authenticated and request_has_role(request, *MODERATION_ROLES)


def can_pin(request: HttpRequest) -> bool:
    """Same set as moderation. A separate name because they are separate powers conceptually, and
    splitting them later should not mean finding every call site."""
    return can_moderate(request)


def can_edit(request: HttpRequest, notice: Notice) -> bool:
    """The author, and only the author.

    Moderators may delete a post but never rewrite it. Silently editing someone else's text — text
    that may already have comments referring to it — is worse than removing it: the post keeps their
    name on it. Deleting is visible; editing is not.
    """
    return request.user.is_authenticated and notice.author_id == request.user.pk


def can_delete(request: HttpRequest, notice: Notice) -> bool:
    """The author cleans up after themselves; Inspektionen moderate."""
    if not request.user.is_authenticated:
        return False
    return notice.author_id == request.user.pk or can_moderate(request)


def can_delete_comment(request: HttpRequest, comment: NoticeComment) -> bool:
    """The comment's author, or a moderator.

    Deliberately **not** the notice's author: letting people moderate the replies to their own post
    invites exactly the disputes Inspektionen exists to settle, and "he deleted my comment" is a
    worse problem for the kollegium than an unwelcome reply staying up until someone impartial looks
    at it.
    """
    if not request.user.is_authenticated:
        return False
    return comment.author_id == request.user.pk or can_moderate(request)
