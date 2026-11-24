"""Ølkælder's records get their links into GAHK-konto: each shopper's account, and the transfer each
deposit, purchase share and adjustment stands for (filled in by 0009), plus the till's send token and
the interest exemptions. The old `is_valid` flags stay until 0009 has read them."""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("credit", "0004_closed_periods"),
        ("oelkaelder", "0007_warning_messages_name_intern_host"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="adjustment",
            name="transfer",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="credit.transfer",
            ),
        ),
        migrations.AddField(
            model_name="deposit",
            name="transfer",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="credit.transfer",
            ),
        ),
        migrations.AddField(
            model_name="purchaseshare",
            name="moved_transfer",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="credit.transfer",
            ),
        ),
        migrations.AddField(
            model_name="purchaseshare",
            name="transfer",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="credit.transfer",
            ),
        ),
        migrations.AddField(
            model_name="shopper",
            name="credit_account",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="credit.creditaccount",
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="dedupe_token",
            field=models.UUIDField(blank=True, null=True, unique=True),
        ),
        migrations.CreateModel(
            name="InterestExemption",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("reason", models.CharField(max_length=255)),
                ("starts_on", models.DateField()),
                ("ends_on", models.DateField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "credit_account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="credit.creditaccount",
                    ),
                ),
            ],
            options={
                "ordering": ["-starts_on", "-id"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("ends_on__isnull", True),
                            ("ends_on__gte", models.F("starts_on")),
                            _connector="OR",
                        ),
                        name="oelk_exemption_ends_after_it_starts",
                    )
                ],
            },
        ),
    ]
