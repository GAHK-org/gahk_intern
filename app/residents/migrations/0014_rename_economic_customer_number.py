from django.db import migrations


class Migration(migrations.Migration):
    """GAHK does not track alumni as e-conomic Customers — each alumne has their own chart-of-accounts
    GL account instead. A plain rename (not remove+add) so any customerNumber already set in dev is
    preserved under the new name, even though it will need to be replaced with a real accountNumber."""

    dependencies = [
        ("residents", "0013_alumneregningsettings"),
    ]

    operations = [
        migrations.RenameField(
            model_name="resident",
            old_name="economic_customer_number",
            new_name="economic_account_number",
        ),
    ]
