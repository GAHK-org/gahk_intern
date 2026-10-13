"""The comment thread on an event.

No index beyond the FK's own: the thread is read one event at a time and ordered by created_at
within it, which `events.event_id` already serves. And nothing here needs a retention column —
CASCADE from the event is the retention, and the event is deleted a week after it is held. See
EventComment's docstring for why that is the design rather than a gap in it.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EventComment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("body", models.TextField(max_length=1000, verbose_name="Kommentar")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="event_comments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="events.event",
                    ),
                ),
            ],
            options={
                "verbose_name": "Kommentar",
                "verbose_name_plural": "Kommentarer",
                "ordering": ["created_at"],
            },
        ),
    ]
