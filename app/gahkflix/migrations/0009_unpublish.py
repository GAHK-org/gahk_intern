"""A video out for less than a day may be made a draft again, which then counts its days as a draft
from then (Video.draft_since)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("gahkflix", "0008_premieres"),
    ]

    operations = [
        migrations.AddField(
            model_name="video",
            name="draft_since",
            field=models.DateTimeField(blank=True, null=True),
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
                    ("unpublished", "Gjort til kladde igen"),
                    ("binned", "Lagt i papirkurven"),
                    ("restored", "Hentet tilbage fra papirkurven"),
                ],
                max_length=12,
            ),
        ),
    ]
