"""Page types (cms.page_types): Page.page_type and Page.fields, PageVersion.fields, and /optagelse
becomes the first typed page.

The page at the address `optagelse` (made by 0007, or by seed_demo) gets the page type `admissions`
and the old site's admissions texts as its fields, as gahk.dk showed them before the move; its body
is emptied. What it said until now is kept as a version in its history, so an edit made since 0007
can be copied into the fields by hand. A database without such a page gets nothing.
"""

from html import escape

from django.db import migrations, models
from django.utils import timezone

PAGE_TYPE = "admissions"

# The old site's templates (gahk_intern's transition/admissions.html and _criteria.html) as they
# were on 2026-11-18, as the editor writes HTML: no classes, headings as <h2>.
FIELDS = {
    "intro": (
        "<p>Her er dejligt på G. A. Hagemanns Kollegium, og vi er glade for at vise stedet frem. Hvis du er "
        "interesseret i at bo her, skal du <strong>først på en rundvisning</strong> — vi synes, det er vigtigt, "
        "at man som kommende alumne får et lille indblik i husets hverdag, før man søger.</p>\n"
        "<p>Indstillingen består af fire beboere, der varetager kontakten til potentielle nye beboere. Gruppen "
        "står for rundvisning og eventuel optagelse på baggrund af de indsendte ansøgninger.</p>"
    ),
    "criteria": (
        "<h2>Kriterier for at søge ind</h2>\n"
        "<p>Vi modtager ansøgninger fra studerende, der er påbegyndt <strong>3. semester</strong> på en længere "
        "videregående uddannelse - fx DTU, KU, SDU, AU, AAU, RUC, CBS eller KADK. To tredjedele af værelserne "
        "er forbeholdt DTU-studerende, så i perioder kan der kun optages studerende fra DTU.</p>\n"
        "<h2>Husleje</h2>\n"
        "<p>Pt. <strong>5.616 kr. pr. måned</strong> inkl. varme, vand og fuld madordning.</p>\n"
        "<h2>Hvornår er der ledige værelser?</h2>\n"
        "<p>Vi optager løbende nye beboere. Den største udskiftning sker typisk tre måneder efter sommerens "
        "specialeafleveringer; derudover flytter en del ud omkring jul, og ellers sker der drysvise "
        "udflytninger i løbet af foråret og efteråret.</p>\n"
        "<h2>Sprog</h2>\n"
        "<p>Vi foretrækker, at ansøgere er dansktalende, da det gør det nemmere at deltage i kollegielivet.</p>\n"
        "<p><em>We prefer that applicants speak Danish, since daily life at the dormitory is a lot easier to "
        "take part in with an understanding of the spoken language.</em></p>"
    ),
    "quote": (
        "«Valget mellem dem, der kan komme i betragtning, skal almindeligvis træffes under hensyn til de "
        "pågældendes vidnesbyrd, flid, begavelse og trang. To tredjedele af pladserne forbeholdes studerende "
        "ved Den polytekniske Læreanstalt, Danmarks Tekniske Universitet.»"
    ),
    "quote_by": "Gustav Adolph Hagemann",
}


def _version(PageVersion, page, note: str) -> None:  # noqa: ANN001, N803
    PageVersion.objects.create(
        page=page,
        slug=page.slug or "",
        header=page.header,
        body=page.body,
        background_image=page.background_image,
        description=page.description,
        fields=page.fields,
        note=note,
    )


def make_admissions_page(apps, schema_editor) -> None:  # noqa: ANN001
    Page = apps.get_model("cms", "Page")
    PageVersion = apps.get_model("cms", "PageVersion")
    page = Page.objects.filter(slug="optagelse").first()
    if page is None or page.page_type:
        return
    _version(PageVersion, page, "Før siden fik faste felter")
    page.page_type = PAGE_TYPE
    page.fields = FIELDS
    page.body = ""
    page.updated_at = timezone.now()
    page.save(update_fields=["page_type", "fields", "body", "updated_at"])
    _version(PageVersion, page, "Faste felter som den gamle optagelsesside")


def make_ordinary_page(apps, schema_editor) -> None:  # noqa: ANN001
    """Back to one text: the fields one after another, the quote as a quote."""
    Page = apps.get_model("cms", "Page")
    for page in Page.objects.filter(page_type=PAGE_TYPE):
        fields = page.fields or {}
        parts = [fields.get("intro", ""), fields.get("criteria", "")]
        if quote := fields.get("quote", ""):
            by = fields.get("quote_by", "")
            parts.append(
                f"<blockquote><p>{escape(quote)}</p>"
                + (f"<p>- {escape(by)}</p>" if by else "")
                + "</blockquote>"
            )
        page.body = "\n".join(part for part in parts if part)
        page.page_type = ""
        page.fields = {}
        page.save(update_fields=["page_type", "fields", "body"])


class Migration(migrations.Migration):
    dependencies = [
        ("cms", "0010_legacy_image_failure_deleted"),
    ]

    operations = [
        migrations.AddField(
            model_name="page",
            name="page_type",
            field=models.CharField(blank=True, default="", editable=False, max_length=40),
        ),
        migrations.AddField(
            model_name="page",
            name="fields",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.AddField(
            model_name="pageversion",
            name="fields",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(make_admissions_page, make_ordinary_page),
    ]
