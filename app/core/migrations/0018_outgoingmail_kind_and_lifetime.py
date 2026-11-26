"""OutgoingMail's bulk flag becomes a kind (core.mail.KINDS), and every mail gets a lifetime."""

from datetime import timedelta

import django.utils.timezone
from django.db import migrations, models

# core.mail.KINDS' lifetimes when this was written; a migration does not read the live ones.
LIFETIMES = {"interactive": timedelta(hours=1), "normal": timedelta(days=1), "bulk": timedelta(days=3)}


def bulk_to_kind(apps, schema_editor) -> None:  # noqa: ANN001
    OutgoingMail = apps.get_model("core", "OutgoingMail")
    for mail in OutgoingMail.objects.all():
        mail.kind = "bulk" if mail.bulk else "normal"
        mail.expires_at = mail.created_at + LIFETIMES[mail.kind]
        mail.save(update_fields=["kind", "expires_at"])


def kind_to_bulk(apps, schema_editor) -> None:  # noqa: ANN001
    OutgoingMail = apps.get_model("core", "OutgoingMail")
    OutgoingMail.objects.filter(kind="bulk").update(bulk=True)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_outgoingmail"),
    ]

    operations = [
        migrations.AddField(
            model_name="outgoingmail",
            name="kind",
            field=models.CharField(
                choices=[("interactive", "Haster"), ("normal", "Almindelig"), ("bulk", "Masseudsendelse")],
                default="normal",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="outgoingmail",
            name="expires_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.RunPython(bulk_to_kind, kind_to_bulk),
        migrations.RemoveField(
            model_name="outgoingmail",
            name="bulk",
        ),
        migrations.AlterField(
            model_name="outgoingmail",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "I kø"),
                    ("sending", "Sendes"),
                    ("sent", "Sendt"),
                    ("failed", "Fejlede"),
                    ("expired", "Udløbet"),
                    ("unknown", "Ukendt"),
                ],
                default="queued",
                max_length=10,
            ),
        ),
    ]
