"""CalendarFeedToken moves to the calendars app (as CalendarSubscription). STATE ONLY: the table
and its rows stay, and calendars 0002 renames it."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0003_eventcomment_image_alter_eventcomment_body"),
        ("calendars", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.DeleteModel(name="CalendarFeedToken")],
            database_operations=[],
        ),
    ]
