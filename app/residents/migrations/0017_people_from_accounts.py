"""Every account but the photo import script's becomes a person with the same id, carrying the facts
about the human (name, birthday, dates, studie, fylgje), and is linked to it.

The same id is what lets 0018 point Residency.resident at the person without rewriting a single
row: every month on the list already names the right id.

Backwards copies the facts onto the accounts again. It is only possible while each person on a list
is still the account with their id, as right after this ran; once a person without a login is on a
list, or new accounts and people have drifted apart, it refuses rather than mixing people up.
"""

from django.conf import settings
from django.core.management.color import no_style
from django.db import migrations
from django.db.models import F

FACTS = ("birthday", "move_in_date", "move_out_date", "study", "fylgje_raw")


def forwards(apps, schema_editor):  # noqa: ANN001, ANN201
    Resident = apps.get_model("residents", "Resident")
    Person = apps.get_model("residents", "Person")
    system = getattr(settings, "PHOTO_ALBUM_SYSTEM_IMPORT_EMAIL", "")
    accounts = list(Resident.objects.exclude(email=system))
    Person.objects.bulk_create(
        [
            Person(
                id=a.id, first_name=a.first_name, last_name=a.last_name, **{f: getattr(a, f) for f in FACTS}
            )
            for a in accounts
        ]
    )
    for a in accounts:
        if a.sponsor_id:
            Person.objects.filter(pk=a.id).update(sponsor_id=a.sponsor_id)
        Resident.objects.filter(pk=a.id).update(person_id=a.id)
    with schema_editor.connection.cursor() as cursor:
        for sql in schema_editor.connection.ops.sequence_reset_sql(no_style(), [Person]):
            cursor.execute(sql)


def backwards(apps, schema_editor):  # noqa: ANN001, ANN201
    Resident = apps.get_model("residents", "Resident")
    Person = apps.get_model("residents", "Person")
    Residency = apps.get_model("residents", "Residency")
    drifted = Residency.objects.exclude(
        resident_id__in=Resident.objects.filter(person_id=F("id")).values("id")
    )
    if drifted.exists():
        raise RuntimeError("People on the alumneliste no longer share their account's id; cannot go back.")
    for person in Person.objects.all():
        Resident.objects.filter(person_id=person.id).update(
            sponsor_id=person.sponsor_id, **{f: getattr(person, f) for f in FACTS}
        )


class Migration(migrations.Migration):
    dependencies = [
        ("residents", "0016_person"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
