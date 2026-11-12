"""Run the stored website HTML through cms.sanitize once.

gahk.dk renders the bodies it is sent as trusted HTML, so everything intern stores must already be
sanitized. The editor cleans what it saves, but rows from before it were imported as they were (the
old etl_cms stored the legacy HTML verbatim). This cleans every page, news item, front-page event and
saved page version once. Each change is recorded as a system change, with the before and after, so
nothing disappears without a trace. Not reversible: the old values stay in the change log.
"""

from django.db import migrations

from cms.sanitize import clean_html

SYSTEM_NAME = "System (HTML renset)"

# (model, field, ContentChange kind, the field holding the row's label); None = no own entry
FIELDS = (
    ("Page", "body", "page", "header"),
    ("NewsItem", "body", "news", "title"),
    ("Event", "description", "event", "title"),
    ("PageVersion", "body", None, None),
)


def clean_stored_html(apps, schema_editor) -> None:  # noqa: ANN001
    ContentChange = apps.get_model("cms", "ContentChange")
    versions: list[int] = []
    for model_name, field, kind, label_field in FIELDS:
        model = apps.get_model("cms", model_name)
        for row in model.objects.exclude(**{field: ""}).only(
            "pk", field, *([label_field] if label_field else [])
        ):
            before = getattr(row, field)
            after = clean_html(before) or ""
            if after == before:
                continue
            model.objects.filter(pk=row.pk).update(**{field: after})
            if kind is None:
                versions.append(row.pk)
                continue
            ContentChange.objects.create(
                by=None,
                by_name=SYSTEM_NAME,
                action="update",
                kind=kind,
                object_id=row.pk,
                label=str(getattr(row, label_field) or "")[:255],
                changes={field: [before, after]},
            )
    if versions:
        ContentChange.objects.create(
            by=None,
            by_name=SYSTEM_NAME,
            action="update",
            kind="page",
            label=f"HTML renset i {len(versions)} gamle sideversioner",
            changes={"page_versions": [None, versions]},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("cms", "0007_optagelse_page"),
    ]

    operations = [migrations.RunPython(clean_stored_html, migrations.RunPython.noop)]
