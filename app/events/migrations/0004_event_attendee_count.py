"""Keep open events after they are held; keep their guest list's size when the list goes.

Schema only. Every existing row gets NULL, which means "the RSVP rows are still here" — true for
all of them, since nothing older than a week survived the old retention. The new retention lives in
EventQuerySet, not here.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0003_eventcomment_image_alter_eventcomment_body"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="attendee_count",
            field=models.PositiveSmallIntegerField(blank=True, editable=False, null=True),
        ),
    ]
