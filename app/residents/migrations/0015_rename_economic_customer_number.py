from django.db import migrations


class Migration(migrations.Migration):
    """GAHK uses per-alumne e-conomic accounts, not Customer records — plain rename to match."""

    dependencies = [
        ("residents", "0014_alumneregningsettings"),
    ]

    operations = [
        migrations.RenameField(
            model_name="resident",
            old_name="economic_customer_number",
            new_name="economic_account_number",
        ),
    ]
