"""Take over begivenheder's calendar tokens: the same table and rows, now owned by this app.

STATE ONLY. The table `events_calendarfeedtoken` already exists and keeps every token, so every
subscribed calendar keeps working; events 0004 drops the model from begivenheder's state, and 0002
here renames the table and adds the new columns.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import calendars.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("events", "0003_eventcomment_image_alter_eventcomment_body"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="CalendarSubscription",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                            ),
                        ),
                        (
                            "token",
                            models.CharField(
                                default=calendars.models.new_token,
                                editable=False,
                                max_length=64,
                                unique=True,
                            ),
                        ),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("rotated_at", models.DateTimeField(blank=True, null=True)),
                        ("last_used_at", models.DateTimeField(blank=True, null=True)),
                        (
                            "resident",
                            models.OneToOneField(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="calendar_subscription",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                    ],
                    options={
                        "verbose_name": "Kalenderabonnement",
                        "verbose_name_plural": "Kalenderabonnementer",
                        "db_table": "events_calendarfeedtoken",
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
