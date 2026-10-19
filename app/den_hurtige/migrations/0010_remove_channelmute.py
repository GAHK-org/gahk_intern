"""Drop ChannelMute: per-room muting is now a notification preference.

Runs AFTER notifications 0002, which copied every mute into a NotificationPreference with push off —
the dependency below is what guarantees the rows are read before the table goes.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("den_hurtige", "0009_quickpost_deleted_at"),
        ("notifications", "0002_channel_mutes_to_preferences"),
    ]

    operations = [
        migrations.DeleteModel(
            name="ChannelMute",
        ),
    ]
