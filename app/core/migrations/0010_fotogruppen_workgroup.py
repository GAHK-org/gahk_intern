"""Create the "Fotogruppen" embedsgruppe so indstilling can assign it (and thereby grant the new
`foto` role via WORKGROUP_ROLE). Idempotent — safe whether or not the group already exists.

Needed because the group never existed: photo_album.access used to ask for a Residency in a
workgroup named "Fotogruppen", and there was no such row in any database, so the check could only
ever pass for administrators. Same shape as 0003_regnskab_workgroup.
"""

from django.db import migrations


def create_fotogruppen(apps, schema_editor) -> None:  # noqa: ANN001
    Workgroup = apps.get_model("core", "Workgroup")
    Workgroup.objects.get_or_create(name="Fotogruppen")


def remove_fotogruppen(apps, schema_editor) -> None:  # noqa: ANN001
    # Only remove it if no one is assigned to it, to avoid clobbering real data on a rollback.
    Workgroup = apps.get_model("core", "Workgroup")
    Workgroup.objects.filter(name="Fotogruppen", residencies__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0009_alter_pushsubscription_wants_opslagstavle")]

    operations = [migrations.RunPython(create_fotogruppen, remove_fotogruppen)]
