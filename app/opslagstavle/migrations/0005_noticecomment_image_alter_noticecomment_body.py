"""One optional photo on a comment, and a body that may therefore be empty.

`body` going blank-able is the half worth pausing on: it widens what the DATABASE accepts without
widening what the feature accepts. A row with neither text nor picture is refused in
views.create_comment, not here, because whether an attached file counts depends on whether it
survived core.uploads - which a field default cannot know. Nothing existing is touched: every
comment written before this has text, and none has an image.

The file is a FileField on the row, not a NoticeImage. NoticeImage exists because a picture embedded
in Markdown has no FK to hang on, so it needs a claiming step and an orphan sweep; this one is owned
by exactly one comment and goes with it through the post_delete receiver in models.py.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("opslagstavle", "0004_author_embedsgruppe"),
    ]

    operations = [
        migrations.AddField(
            model_name="noticecomment",
            name="image",
            field=models.FileField(
                blank=True,
                help_text="Valgfrit billede.",
                max_length=255,
                upload_to="opslag/kommentarer/%Y/%m/",
                verbose_name="Billede",
            ),
        ),
        migrations.AlterField(
            model_name="noticecomment",
            name="body",
            field=models.TextField(blank=True, max_length=1000, verbose_name="Kommentar"),
        ),
    ]
