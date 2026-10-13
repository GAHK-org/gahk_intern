"""Page addresses become real paths, and page edits become recoverable.

`Page.slug` was a SlugField, whose validator forbids "/" — while the router matches the whole
multi-segment path as one key and the ETL seeds `faciliteter/kokken` straight through the ORM. So
the table held addresses the edit form could never accept back. It becomes a CharField validated by
cms.paths.validate_page_path, which permits exactly what config.urls resolves.

Alongside that: PageRedirect (a renamed page's old address keeps working) and PageVersion (a full
snapshot per save, because admin's LogEntry records only which field names changed).

The two RunPython steps are ordered deliberately — see the comments on each.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import cms.paths


def blank_slugs_to_null(apps, schema_editor) -> None:  # noqa: ANN001
    """Turn "" addresses into NULL.

    `unique=True` permits any number of NULLs but only one empty string, so two address-less pages
    could never coexist. The form now stores None, and this brings existing rows into line.

    Runs BEFORE the AlterField: the unique index must never see two "" rows mid-migration.
    """
    Page = apps.get_model("cms", "Page")
    Page.objects.filter(slug="").update(slug=None)


def seed_baseline_versions(apps, schema_editor) -> None:  # noqa: ANN001
    """Give every existing page one snapshot of its current content.

    Runs LAST, once PageVersion exists. Without it the first edit after deploy would have nothing to
    diff against and nothing to restore to — which is the precise gap that made the original
    incident unrecoverable. `created_by` is None because it is genuinely unknown: this content
    predates anyone being recorded as having edited it.
    """
    Page = apps.get_model("cms", "Page")
    PageVersion = apps.get_model("cms", "PageVersion")
    PageVersion.objects.bulk_create(
        [
            PageVersion(
                page=page,
                slug=page.slug or "",
                header=page.header,
                body=page.body,
                background_image=page.background_image,
                created_by=None,
                note="Ved indførsel af versionshistorik",
            )
            for page in Page.objects.all()
        ]
    )


class Migration(migrations.Migration):
    dependencies = [
        ("cms", "0003_cmsimage"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(blank_slugs_to_null, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name="event",
            options={
                "ordering": ["starts_on"],
                "verbose_name": "Forsidebegivenhed",
                "verbose_name_plural": "Forsidebegivenheder",
            },
        ),
        migrations.AlterModelOptions(
            name="newsitem",
            options={
                "ordering": ["-published_at"],
                "verbose_name": "Nyhed",
                "verbose_name_plural": "Nyheder",
            },
        ),
        migrations.AlterModelOptions(
            name="page",
            options={"verbose_name": "Side", "verbose_name_plural": "Sider"},
        ),
        migrations.AlterModelOptions(
            name="pylonevent",
            options={
                "ordering": ["starts_on"],
                "verbose_name": "Pylon-begivenhed",
                "verbose_name_plural": "Pylon-begivenheder",
            },
        ),
        migrations.AlterField(
            model_name="page",
            name="body",
            field=models.TextField(blank=True, verbose_name="Indhold"),
        ),
        migrations.AlterField(
            model_name="page",
            name="header",
            field=models.CharField(max_length=255, verbose_name="Overskrift"),
        ),
        migrations.AlterField(
            model_name="page",
            name="menu_category",
            field=models.PositiveSmallIntegerField(default=0, editable=False),
        ),
        migrations.AlterField(
            model_name="page",
            name="slug",
            field=models.CharField(
                blank=True,
                help_text="Sidens adresse på sitet, fx faciliteter/kokken. Ændrer du den, oprettes der automatisk en omdirigering fra den gamle adresse.",
                max_length=80,
                null=True,
                unique=True,
                validators=[cms.paths.validate_page_path],
                verbose_name="Adresse (URL)",
            ),
        ),
        migrations.CreateModel(
            name="PageRedirect",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "old_path",
                    models.CharField(
                        max_length=80,
                        unique=True,
                        validators=[cms.paths.validate_page_path],
                        verbose_name="Gammel adresse",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="cms_page_redirects",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "page",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="redirects", to="cms.page"
                    ),
                ),
            ],
            options={
                "verbose_name": "Gammel adresse",
                "verbose_name_plural": "Gamle adresser",
                "ordering": ["old_path"],
            },
        ),
        migrations.CreateModel(
            name="PageVersion",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("slug", models.CharField(blank=True, default="", max_length=80)),
                ("header", models.CharField(blank=True, max_length=255)),
                ("body", models.TextField(blank=True)),
                ("background_image", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("note", models.CharField(blank=True, max_length=120)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="cms_page_versions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "page",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="versions",
                        to="cms.page",
                    ),
                ),
            ],
            options={
                "verbose_name": "Sideversion",
                "verbose_name_plural": "Ændringshistorik",
                "ordering": ["-created_at", "-id"],
                "indexes": [
                    models.Index(fields=["page", "-created_at"], name="cms_pagever_page_id_e3ac78_idx")
                ],
            },
        ),
        migrations.RunPython(seed_baseline_versions, migrations.RunPython.noop),
    ]
