"""Accept every administrator who already held the role when the two-person rule landed.

Without this the rule arrives retroactively: nobody has an approved grant, so every administrator
loses Django admin at once — and nobody is left who can approve anyone, because being an approver
requires an approved grant. The gate is for *new* members; the people already inside were admitted
under the old rule and keep what they had.
"""

from django.db import migrations
from django.utils import timezone


def grandfather(apps, schema_editor) -> None:  # noqa: ANN001
    RoleAssignment = apps.get_model("residents", "RoleAssignment")
    AdminAccessGrant = apps.get_model("residents", "AdminAccessGrant")
    now = timezone.now()
    existing = set(AdminAccessGrant.objects.values_list("resident_id", flat=True))
    resident_ids = set(
        RoleAssignment.objects.filter(role="administrator").values_list("resident_id", flat=True)
    )
    AdminAccessGrant.objects.bulk_create(
        [
            AdminAccessGrant(resident_id=rid, status="approved", decided_at=now, created_at=now)
            for rid in sorted(resident_ids - existing)
        ]
    )


def unapply(apps, schema_editor) -> None:  # noqa: ANN001
    """Reversing drops only the rows this migration could have made (decided, but by nobody)."""
    apps.get_model("residents", "AdminAccessGrant").objects.filter(
        status="approved", decided_by__isnull=True
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("residents", "0008_adminaccessgrant")]
    operations = [migrations.RunPython(grandfather, unapply)]
