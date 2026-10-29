import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("gahkflix", "0005_release_date"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="VideoEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("uploaded", "Uploadet"),
                            ("new_version", "Ny version uploadet"),
                            ("processed", "Færdigbehandlet"),
                            ("failed", "Kunne ikke behandles"),
                            ("edited", "Ændret"),
                            ("published", "Udgivet"),
                            ("binned", "Lagt i papirkurven"),
                            ("restored", "Hentet tilbage fra papirkurven"),
                        ],
                        max_length=12,
                    ),
                ),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("changes", models.JSONField(blank=True, default=dict)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="events",
                        to="gahkflix.changerequest",
                    ),
                ),
                (
                    "video",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="gahkflix.video",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-pk"],
            },
        ),
    ]
