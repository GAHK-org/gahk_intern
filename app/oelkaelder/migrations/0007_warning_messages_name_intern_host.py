"""Point the debt-warning e-mails at intern.gahk.dk.

Both seeded warnings (0004) tell the resident their balance is on "Gahk.dk/intern", which is the
public site on other hosting since the intranet moved to intern.gahk.dk. The texts are editable in
the ølkælder admin, so this rewrites whatever is there now, in any spelling of the case.
"""

import re

from django.db import migrations

OLD = re.compile(r"(?:www\.)?gahk\.dk/intern(?![\w-])", re.IGNORECASE)
NEW = re.compile(r"intern\.gahk\.dk", re.IGNORECASE)


def _rewrite(apps, pattern: re.Pattern[str], replacement: str) -> None:  # noqa: ANN001
    Warning = apps.get_model("oelkaelder", "Warning")
    for warning in Warning.objects.all():
        message = pattern.sub(replacement, warning.message)
        if message != warning.message:
            warning.message = message
            warning.save(update_fields=["message"])


def to_intern_host(apps, schema_editor) -> None:  # noqa: ANN001
    _rewrite(apps, OLD, "intern.gahk.dk")


def to_intern_path(apps, schema_editor) -> None:  # noqa: ANN001
    _rewrite(apps, NEW, "gahk.dk/intern")


class Migration(migrations.Migration):
    dependencies = [("oelkaelder", "0006_purchasepolicy")]

    operations = [migrations.RunPython(to_intern_host, to_intern_path)]
