"""Freeze each byline's embedsgruppe onto the post (and the comment) that carries it.

The pill beside a name on Opslagstavlen has to say which group its author was in when they wrote
the thing — see models.AuthoredByResident for why a live lookup would be wrong for almost every
post on a board whose groups rotate monthly.

The backfill is not a guess: a post carries `created_at`, and residents.Residency stores one row
per (resident, month), so the group someone was in when they posted is recorded and simply has to
be read back. Rows with no matching residency — an alumnus, or a month before this kollegium's
list was digitised — keep "", which renders no pill at all.
"""

from django.db import migrations, models
from django.utils import timezone


def backfill_embedsgruppe(apps, schema_editor) -> None:  # noqa: ANN001
    """Stamp every existing post and comment from its own month's residency list.

    One Residency query for the whole table rather than one per row: a two-year board is a few
    hundred posts, but this runs inside a deploy's migrate step, and a few hundred round trips
    against a remote Postgres is a visibly slow deploy for no reason.

    `created_at` is stored in UTC; the month is read in the project's timezone, because "which
    month was this posted in" is a question about the kollegium's calendar, not the database's. A
    post made at 00:30 on the 1st of a month is the only case where the two disagree, and the local
    answer is the one the månedsliste agrees with.
    """
    Residency = apps.get_model("residents", "Residency")
    group_by_person_month = {
        (resident_id, year, month): name
        for resident_id, year, month, name in Residency.objects.filter(workgroup__isnull=False).values_list(
            "resident_id", "year", "month", "workgroup__name"
        )
    }
    if not group_by_person_month:
        return

    for model_name in ("Notice", "NoticeComment"):
        model = apps.get_model("opslagstavle", model_name)
        updated = []
        for row in model.objects.all().only("id", "author_id", "created_at"):
            local = timezone.localtime(row.created_at)
            name = group_by_person_month.get((row.author_id, local.year, local.month))
            if name:
                row.author_embedsgruppe = name
                updated.append(row)
        if updated:
            model.objects.bulk_update(updated, ["author_embedsgruppe"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("opslagstavle", "0003_notice_event"),
        # The backfill reads residents.Residency, so its table has to exist by the time this runs.
        # 0001_initial is enough — the columns read here (resident, year, month, workgroup) are all
        # original — and naming the earliest sufficient migration keeps this from forcing an
        # ordering on unrelated residents changes.
        ("residents", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="notice",
            name="author_embedsgruppe",
            field=models.CharField(
                blank=True,
                help_text="Forfatterens embedsgruppe da opslaget blev skrevet. Sættes automatisk.",
                max_length=100,
                verbose_name="Embedsgruppe",
            ),
        ),
        migrations.AddField(
            model_name="noticecomment",
            name="author_embedsgruppe",
            field=models.CharField(
                blank=True,
                help_text="Forfatterens embedsgruppe da opslaget blev skrevet. Sættes automatisk.",
                max_length=100,
                verbose_name="Embedsgruppe",
            ),
        ),
        # Runs after both AddFields, so there is a column to write into. No reverse: unsetting the
        # snapshots would be the field's own removal, which the AddFields already undo.
        migrations.RunPython(backfill_embedsgruppe, migrations.RunPython.noop),
    ]
