from django.db import migrations, models


class Migration(migrations.Migration):
    # Separate from 0005 so the constraint's ALTER TABLE runs in its own transaction — after 0005's
    # dedup deletes have committed, avoiding Postgres "pending trigger events".

    dependencies = [
        ("rooms", "0005_roomoffer_awarded_application_and_more"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="kvotientapplication",
            constraint=models.UniqueConstraint(
                fields=("resident", "move_month"), name="uniq_resident_move_month"
            ),
        ),
    ]
