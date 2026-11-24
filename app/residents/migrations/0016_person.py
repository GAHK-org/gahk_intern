import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("residents", "0015_delete_roleassignment"),
    ]

    operations = [
        migrations.CreateModel(
            name="Person",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("first_name", models.CharField(max_length=100)),
                ("last_name", models.CharField(blank=True, max_length=100)),
                ("birthday", models.DateField(blank=True, null=True)),
                ("move_in_date", models.DateField(blank=True, null=True)),
                ("move_out_date", models.DateField(blank=True, null=True)),
                ("study", models.CharField(blank=True, max_length=255)),
                ("fylgje_raw", models.CharField(blank=True, max_length=255)),
                (
                    "sponsor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="proteges",
                        to="residents.person",
                    ),
                ),
            ],
            options={"ordering": ["first_name", "last_name"]},
        ),
        migrations.AddField(
            model_name="resident",
            name="person",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="account",
                to="residents.person",
            ),
        ),
    ]
