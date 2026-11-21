"""Premieres: a published video may come out at a set minute, perhaps in the future.

Every video published so far was announced when it was published, so `announced_at` starts as its
`published_at`: the job that announces premieres (services.announce_released) must not tell everybody
about them again.
"""

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import F


def announced_when_published(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    apps.get_model("gahkflix", "Video").objects.filter(published_at__isnull=False).update(
        announced_at=F("published_at")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("gahkflix", "0007_viewings"),
    ]

    operations = [
        migrations.AddField(
            model_name="video",
            name="announced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="video",
            name="premiere_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Premiere"),
        ),
        migrations.AddField(
            model_name="video",
            name="premiere_blur",
            field=models.CharField(
                choices=[
                    ("none", "Ikke sløret"),
                    ("light", "Lidt sløret"),
                    ("medium", "Middel sløret"),
                    ("heavy", "Meget sløret"),
                ],
                default="none",
                max_length=6,
                verbose_name="Miniature før premieren",
            ),
        ),
        migrations.AlterField(
            model_name="videoevent",
            name="kind",
            field=models.CharField(
                choices=[
                    ("uploaded", "Uploadet"),
                    ("new_version", "Ny version uploadet"),
                    ("processed", "Færdigbehandlet"),
                    ("failed", "Kunne ikke behandles"),
                    ("edited", "Ændret"),
                    ("published", "Udgivet"),
                    ("premiered", "Havde premiere"),
                    ("binned", "Lagt i papirkurven"),
                    ("restored", "Hentet tilbage fra papirkurven"),
                ],
                max_length=12,
            ),
        ),
        migrations.RunPython(announced_when_published, migrations.RunPython.noop),
    ]
