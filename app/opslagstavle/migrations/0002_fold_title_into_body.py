"""Drop Notice.title, folding each existing headline into the top of its own body.

A post no longer has a headline: the author is the head of the card (see templates/opslagstavle/
_notice_card.html), which is what every other feed the residents use already does, and what people
kept producing anyway -- titles were mostly a restatement of the first line.

The titles that already exist are content, so they are moved rather than dropped. Each becomes a
bold lead line, which is roughly how it rendered before, and afterwards there is exactly one shape
of post instead of two.

Markdown specials in the title are escaped first. A title is plain text today -- nothing ever
rendered it as Markdown -- so a perfectly ordinary "Kaffe * 3" or "Uge_42" would otherwise start
emphasis inside the bold run and come out mangled or swallow the rest of the line.

The reverse is a no-op on purpose. Splitting a lead line back out of a body cannot be done safely:
by then it is indistinguishable from a bold first line somebody typed themselves, and guessing wrong
would silently delete a line of a post. Reversing therefore restores an empty column, which is
recoverable from a backup; a bad guess is not.
"""

from django.db import migrations

# Escaped in this order -- the backslash first, or it would escape the backslashes added after it.
MARKDOWN_SPECIALS = ("\\", "*", "_", "`", "[", "]", "#")


def _escape(title: str) -> str:
    for char in MARKDOWN_SPECIALS:
        title = title.replace(char, "\\" + char)
    return title


def fold_title_into_body(apps, schema_editor) -> None:  # noqa: ANN001
    Notice = apps.get_model("opslagstavle", "Notice")
    updated = []
    # .only() would still fetch both columns here (they are the two being read), so this is a plain
    # iterator: the board holds a few hundred rows at this kollegium's volume, well within one pass.
    for notice in Notice.objects.exclude(title="").iterator():
        lead = f"**{_escape(notice.title.strip())}**"
        body = notice.body.strip()
        notice.body = f"{lead}\n\n{body}" if body else lead
        updated.append(notice)
    if updated:
        Notice.objects.bulk_update(updated, ["body"], batch_size=200)


class Migration(migrations.Migration):
    dependencies = [("opslagstavle", "0001_initial")]

    operations = [
        # Order matters: fold while the column still exists, then remove it.
        migrations.RunPython(fold_title_into_body, migrations.RunPython.noop),
        migrations.RemoveField(model_name="notice", name="title"),
    ]
