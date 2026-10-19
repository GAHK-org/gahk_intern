"""Label only, and the exact reverse of 0008: the board is called Opslagstavlen again.

Touches no data and no schema. Django wants the migration because verbose_name is part of a field's
recorded state, which is also why there are now two no-op migrations here rather than none: 0008 was
already applied, and deleting an applied migration leaves a `django_migrations` row with no file
behind it. That is not hypothetical in this project - a squashed-away arkiv migration did exactly
that, and the symptom was a `migrate` that reported "no changes to apply" while a column the models
declared was silently missing. A second no-op is much cheaper than that.

The column keeps its name (`wants_opslagstavle`), as 0008 said: it is the topic key that
core.models.TOPIC_FIELDS, core.forms' topic choices and the push bar's `data-topic` attribute all
agree on, none of which a resident ever reads.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_alter_pushsubscription_wants_opslagstavle"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pushsubscription",
            name="wants_opslagstavle",
            field=models.BooleanField(default=False, verbose_name="Opslagstavlen"),
        ),
    ]
