"""One optional photo on an event comment, and a body that may therefore be empty.

Same shape as opslagstavle.0005, and for the same reasons - see that migration's docstring. Under
`begivenheder/` so the whole feature's media sits behind one prefix, and that prefix is not in
core.media.PUBLIC_PREFIXES, so a comment photo needs a login exactly as the event's own image does.

Nothing existing is touched: EventComment shipped one migration ago and every row has text.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0002_eventcomment"),
    ]

    operations = [
        migrations.AddField(
            model_name="eventcomment",
            name="image",
            field=models.FileField(
                blank=True,
                help_text="Valgfrit billede.",
                max_length=255,
                upload_to="begivenheder/kommentarer/%Y/%m/",
                verbose_name="Billede",
            ),
        ),
        migrations.AlterField(
            model_name="eventcomment",
            name="body",
            field=models.TextField(blank=True, max_length=1000, verbose_name="Kommentar"),
        ),
    ]
