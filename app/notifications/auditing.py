"""What the audit trail keeps of notifications: each resident's notification settings, and an
administrator's message to everyone. The notifications themselves are not kept."""

from audit import trail

from .models import Notification, NotificationPreference, NotificationSettings

trail.register(
    NotificationPreference,
    "notifications.preference",
    name=lambda pref: f"{pref.resident.full_name}: {pref.category}",
    label="Notifikationsindstilling",
    labels={
        "resident": "Beboer",
        "category": "Kategori",
        "in_app": "I appen",
        "push": "Push",
        "vibrate": "Vibrer",
    },
)
trail.register(
    NotificationSettings,
    "notifications.settings",
    name=lambda settings: settings.resident.full_name,
    label="Notifikationsopsætning",
    labels={"resident": "Beboer", "vibrate": "Vibrer", "vibrate_min_importance": "Vibrer fra vigtighed"},
)
trail.exempt(
    Notification,
    "delivery bookkeeping: a feature telling people something is part of that feature's own event, "
    "and being read, regrouped and purged after its time is nobody's decision; an administrator's "
    "message to everyone is its own event (notifications.broadcast.sent)",
)

trail.actions(
    {
        "notifications.settings.updated": "Ændrede sine notifikationsindstillinger",
        "notifications.broadcast.sent": "Sendte en notifikation til alle",
    }
)
