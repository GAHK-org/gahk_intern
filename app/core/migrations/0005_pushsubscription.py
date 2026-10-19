"""Move PushSubscription from den_hurtige to core, and give it per-topic consent columns.

A browser has exactly one push endpoint per service-worker registration, so the row is the *device*,
not the feature — and with a second feature (opslagstavlen) notifying through it, it no longer
belongs to whichever feature shipped first.

Three steps, in this order:

  1. `SeparateDatabaseAndState` teaches Django that core now owns the model, touching no SQL. The
     matching state-only DeleteModel is in den_hurtige/0005, which depends on this migration.
  2. `AlterModelTable` performs the real rename, den_hurtige_pushsubscription ->
     core_pushsubscription. One ALTER TABLE on a table with a few dozen rows; Postgres carries the
     indexes, the unique constraint and the FK along with it. Deliberately a real rename rather than
     pinning `db_table` forever: leaving the old app's name in the schema re-creates exactly the
     confusion this move exists to remove, and nothing (no raw SQL, no dashboard) references it.
  3. The two consent columns — both defaulting to False — then a data step opting every
     *pre-existing* row into Den Hurtige. Those devices subscribed when it was the only topic, so
     silently dropping them would look like push breaking.

The split between the default and the UPDATE is deliberate. Both columns default to False because
consent to one feature's notifications is never consent to another's: a True default would hand
every future subscriber the other topic as well, silently. Existing rows are therefore backfilled
explicitly here, which makes this RunPython load-bearing rather than decorative.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def opt_existing_devices_into_den_hurtige(apps, schema_editor) -> None:  # noqa: ANN001
    """Every row that exists when this runs predates topics, so it is a Den Hurtige subscriber.

    Load-bearing, not decorative: the column defaults to False (see the module docstring), so
    without this UPDATE every device already subscribed would silently stop receiving anything.
    """
    PushSubscription = apps.get_model("core", "PushSubscription")
    PushSubscription.objects.update(wants_den_hurtige=True, wants_opslagstavle=False)


def noop(apps, schema_editor) -> None:  # noqa: ANN001
    """Reversing needs no data change: the columns themselves are dropped by the AddField reversal."""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_devclock"),
        ("den_hurtige", "0004_remove_quickreaction_uniq_reaction_per_author_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="PushSubscription",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                            ),
                        ),
                        ("endpoint", models.URLField(max_length=500, unique=True)),
                        ("auth", models.CharField(max_length=100)),
                        ("p256dh", models.CharField(max_length=100)),
                        ("user_agent", models.CharField(blank=True, max_length=500)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        (
                            "user",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="push_subscriptions",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                    ],
                    options={
                        "verbose_name": "Push-abonnement",
                        "verbose_name_plural": "Push-abonnementer",
                        "db_table": "den_hurtige_pushsubscription",
                    },
                ),
            ],
            database_operations=[],
        ),
        migrations.AlterModelTable(name="pushsubscription", table=None),
        migrations.AddField(
            model_name="pushsubscription",
            name="wants_den_hurtige",
            field=models.BooleanField(default=False, verbose_name="Den Hurtige"),
        ),
        migrations.AddField(
            model_name="pushsubscription",
            name="wants_opslagstavle",
            field=models.BooleanField(default=False, verbose_name="Opslagstavlen"),
        ),
        migrations.RunPython(opt_existing_devices_into_den_hurtige, noop),
    ]
