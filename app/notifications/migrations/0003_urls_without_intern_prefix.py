"""Drop the /intern prefix from stored notification targets.

The intranet moved from gahk.dk/intern/ to the root of intern.gahk.dk, so a row written before the
move points at /intern/<rest>. config.urls redirects those anyway, but the open views redirect to
the stored url, and one hop less is one redirect fewer on every tap.

Reversible: backwards puts the prefix back on every site path (the rows written since the move
included), which is what the old URLconf needs.
"""

from django.db import migrations
from django.db.models import Value
from django.db.models.functions import Concat, Length, Substr

PREFIX = "/intern"
URL_MAX_LENGTH = 500


def strip_intern_prefix(apps, schema_editor) -> None:  # noqa: ANN001
    Notification = apps.get_model("notifications", "Notification")
    # "/intern/x"[len("/intern"):] == "/x" — Substr counts from 1.
    Notification.objects.filter(url__startswith=PREFIX + "/").update(url=Substr("url", len(PREFIX) + 1))


def add_intern_prefix(apps, schema_editor) -> None:  # noqa: ANN001
    Notification = apps.get_model("notifications", "Notification")
    (
        Notification.objects.filter(url__startswith="/")
        .exclude(url__startswith="//")
        .annotate(length=Length("url"))
        .filter(length__lte=URL_MAX_LENGTH - len(PREFIX))
        .update(url=Concat(Value(PREFIX), "url"))
    )


class Migration(migrations.Migration):
    dependencies = [("notifications", "0002_channel_mutes_to_preferences")]

    operations = [migrations.RunPython(strip_intern_prefix, add_intern_prefix)]
