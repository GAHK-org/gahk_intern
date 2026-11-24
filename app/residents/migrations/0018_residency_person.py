import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("residents", "0017_people_from_accounts"),
    ]

    operations = [
        # The ids already are the people's (0017), so this only moves the constraint.
        migrations.AlterField(
            model_name="residency",
            name="resident",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="residencies", to="residents.person"
            ),
        ),
        migrations.RemoveField(model_name="resident", name="birthday"),
        migrations.RemoveField(model_name="resident", name="fylgje_raw"),
        migrations.RemoveField(model_name="resident", name="move_in_date"),
        migrations.RemoveField(model_name="resident", name="move_out_date"),
        migrations.RemoveField(model_name="resident", name="sponsor"),
        migrations.RemoveField(model_name="resident", name="study"),
    ]
