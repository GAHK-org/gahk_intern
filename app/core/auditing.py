"""What the audit trail keeps of the shared models (core). Only the outbox so far: the rest of core
is still to do (tests.test_audit.PENDING_APPS)."""

from audit import trail

from .models import OutgoingMail

trail.exempt(
    OutgoingMail,
    "the outbox's delivery bookkeeping, rewritten by the deliverer on every attempt and shown on its own "
    "page; a person sending a mail again is an event (core.mail.sent_again, regnskab's not_received)",
)

trail.kind_labels({"core.mail": "Mail"})
trail.actions({"core.mail.sent_again": "Sendte mails igen"})
