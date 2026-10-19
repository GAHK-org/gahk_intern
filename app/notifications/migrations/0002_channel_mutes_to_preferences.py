"""Carry Den Hurtige's channel mutes over into notification preferences.

A ChannelMute row meant "no push from this channel". The same choice is now the push toggle of the
room's category (notifications.categories.chat_room_key), so every mute becomes a preference row
with push off. Vibration and in-app are left at the default: a room category is push only, and
the mute said nothing about vibration.

den_hurtige's own migration deletes the ChannelMute table after this has run.
"""

from django.db import migrations

CHAT_ROOM_PREFIX = "chat.room."


def forwards(apps, schema_editor) -> None:  # noqa: ANN001
    ChannelMute = apps.get_model("den_hurtige", "ChannelMute")
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")
    NotificationPreference.objects.bulk_create(
        [
            NotificationPreference(
                resident_id=mute.resident_id, category=f"{CHAT_ROOM_PREFIX}{mute.channel}", push=False
            )
            for mute in ChannelMute.objects.all()
        ],
        ignore_conflicts=True,
    )


def backwards(apps, schema_editor) -> None:  # noqa: ANN001
    ChannelMute = apps.get_model("den_hurtige", "ChannelMute")
    NotificationPreference = apps.get_model("notifications", "NotificationPreference")
    ChannelMute.objects.bulk_create(
        [
            ChannelMute(resident_id=pref.resident_id, channel=pref.category.removeprefix(CHAT_ROOM_PREFIX))
            for pref in NotificationPreference.objects.filter(
                category__startswith=CHAT_ROOM_PREFIX, push=False
            )
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0001_initial"),
        ("den_hurtige", "0009_quickpost_deleted_at"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
