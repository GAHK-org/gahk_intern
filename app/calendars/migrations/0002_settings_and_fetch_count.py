"""Rename the moved table, and add the subscription's choices and its fetch count.

Existing subscriptions get the defaults, which is everything on: every event they can see (marked
when unanswered) and the birthdays, as for anybody subscribing from now on.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("calendars", "0001_initial"),
        ("events", "0004_move_calendar_feed_token"),
    ]

    operations = [
        migrations.AlterModelTable(name="calendarsubscription", table=None),
        migrations.AddField(
            model_name="calendarsubscription",
            name="events",
            field=models.CharField(
                choices=[("all", "Alle begivenheder du kan se"), ("accepted", "Kun dem du har sagt ja til")],
                default="all",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="calendarsubscription",
            name="include_birthdays",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="calendarsubscription",
            name="fetch_count",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
