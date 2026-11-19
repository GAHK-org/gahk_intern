"""The admissions landing text becomes an ordinary page at /optagelse.

It was hard-coded in templates/optagelse/landing.html and _criteria.html, so the rent and the
criteria could only be changed by a developer. gahk.dk now renders the page like any other, and the
two forms it links to (/optagelse/ansoeg and /optagelse/fremlej) are gahk.dk's own.

Created only when the database already holds pages and none has the address `optagelse`. An empty
database (a fresh dev or CI setup) gets nothing: its page ids are about to come from the legacy ETL
or seed_demo, and a row created here would take id 1, the front page's.
"""

from django.core.management.color import no_style
from django.db import migrations
from django.utils import timezone

HEADER = "Optagelse"
DESCRIPTION = "Sådan søger du ind på G. A. Hagemanns Kollegium: kriterier, husleje, rundvisning og fremleje."

# Copied from the templates as they were on 2026-11-12, as plain HTML: no template syntax, no CSS
# classes (gahk.dk styles page bodies itself).
BODY = """<p>Her er dejligt på G. A. Hagemanns Kollegium, og vi er glade for at vise stedet frem. Hvis du er interesseret i at bo her, skal du <strong>først på en rundvisning</strong> — vi synes, det er vigtigt, at man som kommende alumne får et lille indblik i husets hverdag, før man søger.</p>
<p>Indstillingen består af fire beboere, der varetager kontakten til potentielle nye beboere. Gruppen står for rundvisning og eventuel optagelse på baggrund af de indsendte ansøgninger.</p>
<h2>Sådan foregår det</h2>
<ol>
<li><strong>Tjek kriterierne.</strong> Se nedenfor, om du opfylder betingelserne for at søge ind.</li>
<li><strong>Anmod om rundvisning.</strong> Udfyld formularen. Vi kontakter dig, hvis der er udsigt til ledige værelser, og din profil passer.</li>
<li><strong>Send ansøgning.</strong> Efter rundvisningen sender vi dig et ansøgningsskema til en motiveret ansøgning, hvis du kunne tænke dig at bo sammen med os.</li>
</ol>
<h2>Kriterier for at søge ind</h2>
<p>Vi modtager ansøgninger fra studerende, der er påbegyndt <strong>3. semester</strong> på en længere videregående uddannelse - fx DTU, KU, SDU, AU, AAU, RUC, CBS eller KADK. To tredjedele af værelserne er forbeholdt DTU-studerende, så i perioder kan der kun optages studerende fra DTU.</p>
<h2>Husleje</h2>
<p>Pt. <strong>5.616 kr. pr. måned</strong> inkl. varme, vand og fuld madordning.</p>
<h2>Hvornår er der ledige værelser?</h2>
<p>Vi optager løbende nye beboere. Den største udskiftning sker typisk tre måneder efter sommerens specialeafleveringer; derudover flytter en del ud omkring jul, og ellers sker der drysvise udflytninger i løbet af foråret og efteråret.</p>
<h2>Sprog</h2>
<p>Vi foretrækker, at ansøgere er dansktalende, da det gør det nemmere at deltage i kollegielivet.</p>
<p><em>We prefer that applicants speak Danish, since daily life at the dormitory is a lot easier to take part in with an understanding of the spoken language.</em></p>
<blockquote>
<p>«Valget mellem dem, der kan komme i betragtning, skal almindeligvis træffes under hensyn til de pågældendes vidnesbyrd, flid, begavelse og trang. To tredjedele af pladserne forbeholdes studerende ved Den polytekniske Læreanstalt, Danmarks Tekniske Universitet.»</p>
<p>- Gustav Adolph Hagemann</p>
</blockquote>
<h2>Søg</h2>
<ul>
<li><a href="/optagelse/ansoeg">Anmod om rundvisning</a> — for dig, der gerne vil søge om at bo fast på kollegiet.</li>
<li><a href="/optagelse/fremlej">Ansøg om fremleje</a> — skal du bo her midlertidigt via en nuværende beboer?</li>
</ul>"""


def create_admissions_page(apps, schema_editor) -> None:  # noqa: ANN001
    Page = apps.get_model("cms", "Page")
    PageVersion = apps.get_model("cms", "PageVersion")
    if not Page.objects.exists() or Page.objects.filter(slug="optagelse").exists():
        return
    # The legacy ETL inserts explicit ids; if the id sequence was not reset after it, the next id it
    # hands out is one that is taken. Reset it first (a no-op where it is already right).
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        for sql in connection.ops.sequence_reset_sql(no_style(), [Page, PageVersion]):
            cursor.execute(sql)
    page = Page.objects.create(
        slug="optagelse",
        header=HEADER,
        body=BODY,
        description=DESCRIPTION,
        updated_at=timezone.now(),
    )
    PageVersion.objects.create(
        page=page,
        slug=page.slug,
        header=page.header,
        body=page.body,
        description=page.description,
        note="Oprettet fra den faste optagelsesside",
    )


def remove_admissions_page(apps, schema_editor) -> None:  # noqa: ANN001
    """Only the page this migration made, and only while nobody has edited it."""
    Page = apps.get_model("cms", "Page")
    Page.objects.filter(slug="optagelse", header=HEADER, body=BODY).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("cms", "0006_seed_website"),
    ]

    operations = [
        migrations.RunPython(create_admissions_page, remove_admissions_page),
    ]
