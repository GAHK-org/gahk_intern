"""Accounts, people and residents come apart.

The login model, Resident until now, is Account (the history in 0001-0012 was renamed in place, with
its table kept); here it gets a table of its own, `residents_account`, which frees the name Resident
for a person's membership of the house.

Every account becomes a person (the human: name, birthday, phone) and a resident (their time here:
move-in, move-out, studie, fylgje), all three with the same id, except the photo import script's and
the shared kitchen login, which are no one. The same id is what lets the months on the alumneliste
point at the resident without rewriting a row.

Backwards copies the facts onto the accounts again. It refuses once the ids have drifted apart (a
resident with no login on a list, a person made after this ran), rather than mixing people up.
"""

import django.db.models.deletion
from django.conf import settings
from django.core.management.color import no_style
from django.db import migrations, models

# The automatic many-to-many tables with a column named after the login model.
RENAME_M2M_COLUMNS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'residents_account_groups' AND column_name = 'resident_id') THEN
    ALTER TABLE residents_account_groups RENAME COLUMN resident_id TO account_id;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'residents_account_user_permissions' AND column_name = 'resident_id') THEN
    ALTER TABLE residents_account_user_permissions RENAME COLUMN resident_id TO account_id;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'events_event_co_organisers' AND column_name = 'resident_id') THEN
    ALTER TABLE events_event_co_organisers RENAME COLUMN resident_id TO account_id;
  END IF;
END $$;
"""
RENAME_M2M_COLUMNS_BACK = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'residents_account_groups' AND column_name = 'account_id') THEN
    ALTER TABLE residents_account_groups RENAME COLUMN account_id TO resident_id;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'residents_account_user_permissions' AND column_name = 'account_id') THEN
    ALTER TABLE residents_account_user_permissions RENAME COLUMN account_id TO resident_id;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'events_event_co_organisers' AND column_name = 'account_id') THEN
    ALTER TABLE events_event_co_organisers RENAME COLUMN account_id TO resident_id;
  END IF;
END $$;
"""

PERSON_FACTS = ("phone", "birthday")
RESIDENT_FACTS = ("move_in_date", "move_out_date", "study", "fylgje_raw")


def rename_content_type(apps, schema_editor):  # noqa: ANN001, ANN201
    """The login's content type was (residents, resident); it is (residents, account) now, and the
    new Resident gets a content type of its own. Where both exist already (a database migrated back
    and forth), what points at the old one moves to the new one first."""
    ContentType = apps.get_model("contenttypes", "ContentType")
    old = ContentType.objects.filter(app_label="residents", model="resident").first()
    if old is None:
        return
    new = ContentType.objects.filter(app_label="residents", model="account").first()
    if new is None:
        ContentType.objects.filter(pk=old.pk).update(model="account")
        return
    Permission = apps.get_model("auth", "Permission")
    taken = set(Permission.objects.filter(content_type=new).values_list("codename", flat=True))
    Permission.objects.filter(content_type=old).exclude(codename__in=taken).update(content_type=new)
    apps.get_model("admin", "LogEntry").objects.filter(content_type=old).update(content_type=new)
    ContentType.objects.filter(pk=old.pk).delete()


def rename_content_type_back(apps, schema_editor):  # noqa: ANN001, ANN201
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="residents", model="resident").delete()
    ContentType.objects.filter(app_label="residents", model="account").update(model="resident")


def people_from_accounts(apps, schema_editor):  # noqa: ANN001, ANN201
    Account = apps.get_model("residents", "Account")
    Person = apps.get_model("residents", "Person")
    Resident = apps.get_model("residents", "Resident")
    system = getattr(settings, "PHOTO_ALBUM_SYSTEM_IMPORT_EMAIL", "")
    accounts = list(Account.objects.exclude(email=system).exclude(is_kitchen=True))
    Person.objects.bulk_create(
        [
            Person(
                id=a.id,
                first_name=a.first_name,
                last_name=a.last_name,
                **{f: getattr(a, f) for f in PERSON_FACTS},
            )
            for a in accounts
        ]
    )
    Resident.objects.bulk_create(
        [Resident(id=a.id, person_id=a.id, **{f: getattr(a, f) for f in RESIDENT_FACTS}) for a in accounts]
    )
    ids = {a.id for a in accounts}
    for a in accounts:
        if a.sponsor_id in ids:
            Resident.objects.filter(pk=a.id).update(sponsor_id=a.sponsor_id)
        Account.objects.filter(pk=a.id).update(person_id=a.id)
    with schema_editor.connection.cursor() as cursor:
        for sql in schema_editor.connection.ops.sequence_reset_sql(no_style(), [Person, Resident]):
            cursor.execute(sql)


def people_back_onto_accounts(apps, schema_editor):  # noqa: ANN001, ANN201
    Account = apps.get_model("residents", "Account")
    Resident = apps.get_model("residents", "Resident")
    Residency = apps.get_model("residents", "Residency")
    drifted = Resident.objects.exclude(id=models.F("person_id")) | Resident.objects.exclude(
        person_id__in=Account.objects.filter(person_id=models.F("id")).values("person_id")
    )
    if drifted.exists() and Residency.objects.filter(resident__in=drifted).exists():
        raise RuntimeError("Residents on the alumneliste no longer share their account's id; cannot go back.")
    for resident in Resident.objects.select_related("person"):
        Account.objects.filter(pk=resident.person_id).update(
            sponsor_id=resident.sponsor_id,
            **{f: getattr(resident, f) for f in RESIDENT_FACTS},
            **{f: getattr(resident.person, f) for f in PERSON_FACTS},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("admin", "0003_logentry_add_action_flag_choices"),
        ("residents", "0012_resident_economic_account_number"),
    ]

    operations = [
        migrations.RunPython(rename_content_type, rename_content_type_back),
        migrations.AlterModelTable(name="account", table=None),
        # Django names the columns of the automatic many-to-many tables after the model, so a
        # database made while the login was Resident has `resident_id` where the history now says
        # `account_id`. Renamed where it is so; a database made from this history has it already.
        migrations.RunSQL(RENAME_M2M_COLUMNS, RENAME_M2M_COLUMNS_BACK),
        migrations.CreateModel(
            name="Person",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("first_name", models.CharField(max_length=100)),
                ("last_name", models.CharField(blank=True, max_length=100)),
                ("birthday", models.DateField(blank=True, null=True)),
                ("phone", models.CharField(blank=True, max_length=40)),
            ],
            options={"ordering": ["first_name", "last_name"]},
        ),
        migrations.CreateModel(
            name="Resident",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("move_in_date", models.DateField(blank=True, null=True)),
                ("move_out_date", models.DateField(blank=True, null=True)),
                ("study", models.CharField(blank=True, max_length=255)),
                ("fylgje_raw", models.CharField(blank=True, max_length=255)),
                (
                    "person",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="resident",
                        to="residents.person",
                    ),
                ),
                (
                    "sponsor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="proteges",
                        to="residents.resident",
                    ),
                ),
            ],
            options={"ordering": ["person__first_name", "person__last_name"]},
        ),
        migrations.AddField(
            model_name="account",
            name="person",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="account",
                to="residents.person",
            ),
        ),
        migrations.RunPython(people_from_accounts, people_back_onto_accounts),
        # The ids already are the residents' (above), so this only moves the constraint.
        migrations.AlterField(
            model_name="residency",
            name="resident",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="residencies",
                to="residents.resident",
            ),
        ),
        migrations.RemoveField(model_name="account", name="phone"),
        migrations.RemoveField(model_name="account", name="birthday"),
        migrations.RemoveField(model_name="account", name="move_in_date"),
        migrations.RemoveField(model_name="account", name="move_out_date"),
        migrations.RemoveField(model_name="account", name="study"),
        migrations.RemoveField(model_name="account", name="sponsor"),
        migrations.RemoveField(model_name="account", name="fylgje_raw"),
    ]
