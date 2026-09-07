"""Label only: the noticeboard is called Ankebogen to residents now.

The COLUMN keeps its name. `wants_opslagstavle` is the app package's name for the topic and it is
wired to it in three places that have nothing to do with what a resident reads — core.models'
TOPIC_FIELDS, core.forms' topic choices, and the `data-topic` attribute the push bar posts back —
so renaming it would be a data migration plus a coordinated frontend change to relabel a checkbox
in the admin. Nothing here touches the database; Django only wants the migration because
verbose_name is part of the field's recorded state.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0007_pushsubscription_wants_reparationer"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pushsubscription",
            name="wants_opslagstavle",
            field=models.BooleanField(default=False, verbose_name="Ankebogen"),
        ),
    ]
