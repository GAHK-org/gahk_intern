"""Money lives only in GAHK-konto now: the old `is_valid` flags go (a voided record is one whose
transfer is reversed), every shopper has their account, and the purchase limit policy goes (the
account's own credit limit decides)."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("oelkaelder", "0009_history_into_the_ledger"),
    ]

    operations = [
        migrations.RemoveField(model_name="adjustment", name="is_valid"),
        migrations.RemoveField(model_name="deposit", name="is_valid"),
        migrations.RemoveField(model_name="transaction", name="is_valid"),
        migrations.AlterField(
            model_name="shopper",
            name="credit_account",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT, related_name="+", to="credit.creditaccount"
            ),
        ),
        migrations.DeleteModel(name="PurchasePolicy"),
    ]
