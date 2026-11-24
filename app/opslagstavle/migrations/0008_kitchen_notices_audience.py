"""The kitchen staff's own posts are for the house and the kitchen.

Until audiences, the kitchen saw only its own posts and the house saw everything. Every post is now
for the whole house (0007); the kitchen's own are for the kitchen too, so its staff keep seeing them.
Back: nothing to undo, 0007's removal drops the column.
"""

from django.db import migrations

HOUSE_AND_KITCHEN = sorted(
    [
        "moving_in",
        "resident",
        "moved_out_under_1_month",
        "moved_out_1_to_3_months",
        "moved_out_3_to_12_months",
        "moved_out_over_a_year",
        "kitchen",
    ]
)


def forwards(apps, schema_editor):  # noqa: ANN001, ANN201
    Notice = apps.get_model("opslagstavle", "Notice")
    Grant = apps.get_model("access", "Grant")
    kitchen = Grant.objects.filter(role="kitchen").values("resident_id")
    Notice.objects.filter(author_id__in=kitchen).update(audience=HOUSE_AND_KITCHEN)


class Migration(migrations.Migration):
    dependencies = [
        ("opslagstavle", "0007_notice_audience"),
        ("access", "0002_shared_kitchen_login"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
