"""The shared kitchen login's flag (Account.is_kitchen) becomes a grant of the kitchen role.

The kitchen staff get accounts of their own on the Adgang page; the shared one keeps working until
somebody ends its grant there, so the kitchen is never locked out by a deploy. Back: the flag again
for every account holding the kitchen role by a grant that has not ended.
"""

from django.db import migrations
from django.utils import timezone

REASON = "Fælles køkkenlogin (afløses af personlige konti)"


def forwards(apps, schema_editor):  # noqa: ANN001, ANN201
    Account = apps.get_model("residents", "Account")
    Grant = apps.get_model("access", "Grant")
    today = timezone.localdate()
    for pk in Account.objects.filter(is_kitchen=True).values_list("pk", flat=True):
        Grant.objects.get_or_create(
            account_id=pk, role="kitchen", ends_on=None, defaults={"reason": REASON, "starts_on": today}
        )


def backwards(apps, schema_editor):  # noqa: ANN001, ANN201
    Account = apps.get_model("residents", "Account")
    Grant = apps.get_model("access", "Grant")
    kitchen = Grant.objects.filter(role="kitchen", ends_on__isnull=True).values("account_id")
    Account.objects.filter(pk__in=kitchen).update(is_kitchen=True)


class Migration(migrations.Migration):
    dependencies = [
        ("access", "0003_access_page_permissions"),
        ("residents", "0015_delete_roleassignment"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
