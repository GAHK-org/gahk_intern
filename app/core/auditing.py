"""What the audit trail keeps of the shared models (core): the rooms, embedsgrupper and rengøringer
everything else refers to, and the devices residents get push notifications on. The rest is the
system's own bookkeeping, left out below with the reason."""

from audit import trail

from .models import (
    Cleaning,
    DevClock,
    OutgoingMail,
    PushDeliveryDay,
    PushSubscription,
    Room,
    ServiceHeartbeat,
    WorkerJobLog,
    Workgroup,
)

# Reference data, loaded from the old site (seed_rooms, etl_residents) and edited in the Django admin,
# where every change is recorded as "Uden handling".
trail.register(
    Room,
    "core.room",
    name=str,
    label="Værelse",
    labels={
        "legacy_index": "Gammelt nummer",
        "number": "Nummer",
        "floor": "Etage",
        "side": "Side",
        "note": "Note",
    },
)
trail.register(
    Workgroup,
    "core.workgroup",
    name=lambda group: group.name,
    label="Embedsgruppe",
    labels={"legacy_id": "Gammelt id", "name": "Navn", "size": "Antal"},
)
trail.register(
    Cleaning,
    "core.cleaning",
    name=lambda cleaning: cleaning.name,
    label="Rengøring",
    labels={"legacy_id": "Gammelt id", "name": "Navn", "size": "Antal"},
)
trail.register(
    PushSubscription,
    "core.push_subscription",
    name=lambda device: f"{device.user.full_name}: {device.user_agent[:80] or 'ukendt browser'}",
    label="Push-enhed",
    # Delivery bookkeeping, moved by every push.
    ignore={"created_at", "last_success_at", "failure_count"},
    # The device's address at the push service and its encryption keys: what it takes to push to it.
    redact={"endpoint", "auth", "p256dh"},
    labels={
        "user": "Beboer",
        "endpoint": "Adresse",
        "auth": "Nøgle (auth)",
        "p256dh": "Nøgle (p256dh)",
        "user_agent": "Browser",
    },
)

trail.exempt(
    OutgoingMail,
    "the outbox's delivery bookkeeping, rewritten by the deliverer on every attempt and shown on its own "
    "page; a person sending a mail again is an event (core.mail.sent_again, regnskab's not_received)",
)
trail.exempt(WorkerJobLog, "the worker's own log of each job, shown on the worker-jobs page")
trail.exempt(PushDeliveryDay, "push delivery counters per day, for the notification statistics")
trail.exempt(ServiceHeartbeat, "when a worker or beat last said it was alive, rewritten on every beat")
trail.exempt(DevClock, "a simulated date for local testing, never read in production")

trail.kind_labels({"core.mail": "Mail"})
trail.actions(
    {
        "core.mail.sent_again": "Sendte mails igen",
        "core.push_subscription.subscribed": "Slog push-notifikationer til på en enhed",
        "core.push_subscription.unsubscribed": "Slog push-notifikationer fra på en enhed",
        "core.push_subscription.cancelled": "Annullerede en enheds push-abonnement",
        "core.push_subscription.expired": "Push-tjenesten havde glemt enheden",
    }
)
