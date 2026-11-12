"""Data for the Hjemmeside content model.

  * Titles become plain text. Page.header, NewsItem.title and the front-page Event.title were
    rendered `|safe` without ever being sanitized; from now on they are escaped everywhere, so any
    markup or entity in them is turned into the text it displayed as.
  * Page.updated_at starts as the time of the page's newest version (when there is one), so
    "Sidst rettet" means something from day one.
  * The menu, which used to be a hard-coded list (core.context_processors.NAV_LEGACY), becomes
    MenuItem rows with the same labels, links and order.
  * The two hero images that were hard-coded in templates become content: SiteSettings'
    default background (every page without its own) and the front page's own background. They still
    point at the legacy files here; cms.legacy_images moves them into the image library on the
    first publish.

Every step leaves data alone that somebody has already set, so it is safe on any database.
"""

import html
import re

from django.db import migrations

# Copied, not imported: a migration must keep meaning what it meant when it was written.
NAV_LEGACY = [
    ("/", "Hjem"),
    ("/kollegielivet/historie", "Historie"),
    ("/vision", "Vision"),
    ("/kollegielivet", "Kollegielivet"),
    ("/faciliteter", "Faciliteter"),
    ("/begivenheder/", "Begivenheder"),
    ("/optagelse/", "Optagelse"),
    ("/legater", "Legater"),
    ("/kontakt", "Kontakt"),
]
DEFAULT_BACKGROUND = "/public/image/upload/images/gang.jpg"
HOME_HERO = "/public/image/upload/images/72352712_3043170802378120_6023122459278966784_n.jpg"
FRONT_PAGE_ID = 1

TAG_RE = re.compile(r"<[^>]*>")


def _plain(value: str) -> str:
    return " ".join(html.unescape(TAG_RE.sub(" ", value)).split())


def titles_to_plain_text(apps, schema_editor) -> None:  # noqa: ANN001
    for model_name, field_name in (("Page", "header"), ("NewsItem", "title"), ("Event", "title")):
        model = apps.get_model("cms", model_name)
        for row in model.objects.only("pk", field_name):
            value = getattr(row, field_name)
            plain = _plain(value)[:255]
            if plain != value and plain:
                setattr(row, field_name, plain)
                row.save(update_fields=[field_name])


def updated_at_from_versions(apps, schema_editor) -> None:  # noqa: ANN001
    Page = apps.get_model("cms", "Page")
    PageVersion = apps.get_model("cms", "PageVersion")
    for page in Page.objects.all():
        newest = PageVersion.objects.filter(page=page).order_by("-created_at", "-id").first()
        if newest is not None:
            Page.objects.filter(pk=page.pk).update(updated_at=newest.created_at, updated_by=newest.created_by)


def seed_menu(apps, schema_editor) -> None:  # noqa: ANN001
    MenuItem = apps.get_model("cms", "MenuItem")
    if MenuItem.objects.exists():
        return
    MenuItem.objects.bulk_create(
        [MenuItem(label=label, url=url, order=index * 10) for index, (url, label) in enumerate(NAV_LEGACY)]
    )


def unseed_menu(apps, schema_editor) -> None:  # noqa: ANN001
    MenuItem = apps.get_model("cms", "MenuItem")
    seeded = [(url, label) for url, label in NAV_LEGACY]
    if [(m.url, m.label) for m in MenuItem.objects.order_by("order", "id")] == seeded:
        MenuItem.objects.all().delete()


def seed_backgrounds(apps, schema_editor) -> None:  # noqa: ANN001
    SiteSettings = apps.get_model("cms", "SiteSettings")
    Page = apps.get_model("cms", "Page")
    site, _created = SiteSettings.objects.get_or_create(pk=1)
    if not site.default_background:
        site.default_background = DEFAULT_BACKGROUND
        site.save(update_fields=["default_background"])
    Page.objects.filter(pk=FRONT_PAGE_ID, background_image="").update(background_image=HOME_HERO)


def unseed_backgrounds(apps, schema_editor) -> None:  # noqa: ANN001
    SiteSettings = apps.get_model("cms", "SiteSettings")
    Page = apps.get_model("cms", "Page")
    SiteSettings.objects.filter(pk=1, default_background=DEFAULT_BACKGROUND).delete()
    Page.objects.filter(pk=FRONT_PAGE_ID, background_image=HOME_HERO).update(background_image="")


class Migration(migrations.Migration):
    dependencies = [
        ("cms", "0005_hjemmeside_content_model"),
    ]

    operations = [
        migrations.RunPython(titles_to_plain_text, migrations.RunPython.noop),
        migrations.RunPython(updated_at_from_versions, migrations.RunPython.noop),
        migrations.RunPython(seed_menu, unseed_menu),
        migrations.RunPython(seed_backgrounds, unseed_backgrounds),
    ]
