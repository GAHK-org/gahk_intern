"""Demo Drive for `manage.py seed_demo`: folders and real files in every space, for every login.

What each documented login (password demo1234) finds:

  * admin@ and formand@ oversee (administrator holds Inspektionen's role): every group's
    `Uden periode` and earlier periods to read, and the name, size and file count of each group's
    current period without its contents. Only they could make Fælles' first-level folders
    (`Vejledninger`, `Skabeloner`), so formand@ made them.
  * regnskab@, ak@ and oel@ write their own group's `Uden periode` and current period, and the
    previous period they were in; they read the period before that and see no other group.
  * beboer@ has a note in this month's group (whichever the residency seed put them in) and sees
    only the groups they have been in over the seeded months, not the others.
  * Everyone living here reads all of Fælles and writes in `Uden årstal` below its first level, this
    year and last year. The year before last is there to be read-only.

The system only makes the current period and year (drive.structure: no back-filling), so the
earlier periods and years here are made by hand, as they would have been made in their time.

Files go through drive.services as an upload would, each step run at the time it happened (`_at`),
so sizes, versions and "last changed by" agree, and the bytes are real: an image opens, a PDF
renders and a spreadsheet opens in Excel.
"""

import csv
import mimetypes
import random
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from io import BytesIO, StringIO
from unittest import mock

from django.core.files.base import ContentFile
from django.utils import timezone

from core.models import Workgroup
from residents.models import Residency, Resident, Role
from residents.permissions import has_active_role

from . import services
from .access import Viewer
from .models import File, FileVersion, Folder, Space
from .periods import Period, period_containing
from .structure import COMMON_TIMELESS, ensure_structure, folder_name

LOGINS = (
    "admin@gahk.dk",
    "formand@gahk.dk",
    "ak@gahk.dk",
    "oel@gahk.dk",
    "regnskab@gahk.dk",
    "beboer@gahk.dk",
)


@contextmanager
def _at(when: datetime) -> Iterator[None]:
    """Run a step as if it were `when`. Drive stamps its rows with timezone.now() in many places
    (created_at, changed_at, the version's time), so this keeps them all in agreement rather than
    rewriting each column afterwards. Viewers are built outside it: their period is today's."""
    with mock.patch("django.utils.timezone.now", return_value=when):
        yield


class _Seeder:
    def __init__(self, residents: list[Resident], now: datetime) -> None:
        self.now = now
        self.today = timezone.localdate(now)
        self.by_email = {r.email: r for r in residents}
        self._viewers: dict[tuple[int, date], Viewer] = {}

    def login(self, email: str) -> Resident:
        return self.by_email[email]

    def viewer(self, resident: Resident, when: datetime) -> Viewer:
        """What the resident could do on the day: an earlier period was the current one then."""
        day = timezone.localdate(when)
        key = (resident.pk, day)
        if key not in self._viewers:
            oversees = has_active_role(resident, Role.INSPEKTION)
            self._viewers[key] = Viewer.build(resident.pk, oversees=oversees, today=day)
        return self._viewers[key]

    def ago(self, days: int, *, not_before: date | None = None) -> datetime:
        """`days` before now, in working hours, but not before the folder's period began (early in a
        new period) nor after now (late at night)."""
        day = timezone.localdate(self.now - timedelta(days=days))
        when = min(_noon(day) + timedelta(hours=days * 5 % 9 - 3), self.now)
        return max(when, _noon(not_before)) if not_before is not None else when

    # --- folders ---------------------------------------------------------------------------------

    def folder(self, parent: Folder, name: str, by: Resident, when: datetime) -> Folder:
        viewer = self.viewer(by, when)
        with _at(when):
            return services.create_folder(viewer, parent, name, by)

    def path(self, parent: Folder, names: str, by: Resident, when: datetime) -> Folder:
        for name in names.split("/"):
            parent = self.folder(parent, name, by, when)
        return parent

    # --- files -----------------------------------------------------------------------------------

    def upload(self, folder: Folder, name: str, data: bytes, by: Resident, when: datetime) -> File:
        """Store the bytes and add the file, or a new version of it when the name is there."""
        key = services.new_key()
        services.store_local(key, ContentFile(data))
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        viewer = self.viewer(by, when)
        with _at(when):
            added = services.add_upload(
                viewer,
                folder,
                name=name,
                key=key,
                size=len(data),
                content_type=content_type,
                on_conflict=services.REPLACE,
                resident=by,
            )
        return added.file


def _noon(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(12)))


def _group_of(resident: Resident, today: date) -> Workgroup | None:
    residency = (
        Residency.objects.filter(resident=resident, year=today.year, month=today.month)
        .select_related("workgroup")
        .first()
    )
    return residency.workgroup if residency else None


def _group_folder(workgroup: Workgroup, kind: str, period: Period | None = None) -> Folder:
    """A group's timeless or period folder; an earlier period is made as the system would have."""
    fields = {"period_start": period.start} if period else {}
    found = Folder.objects.filter(workgroup=workgroup, kind=kind, **fields).first()
    if found is not None:
        return found
    group = Folder.objects.get(workgroup=workgroup, kind=Folder.Kind.GROUP)
    assert period is not None  # noqa: S101  # nosec B101 - the timeless folder is always made
    folder = Folder(
        parent=group,
        name=period.name,
        kind=Folder.Kind.PERIOD,
        space=Space.GROUP,
        workgroup=workgroup,
        period_start=period.start,
    )
    folder.save()
    return folder


def _year_folder(year: int) -> Folder:
    found = Folder.objects.filter(kind=Folder.Kind.YEAR, period_start=date(year, 1, 1)).first()
    if found is not None:
        return found
    folder = Folder(
        parent=Folder.objects.get(kind=Folder.Kind.COMMON_ROOT),
        name=str(year),
        kind=Folder.Kind.YEAR,
        space=Space.COMMON,
        period_start=date(year, 1, 1),
    )
    folder.save()
    return folder


def seed(residents: list[Resident], now: datetime, _rng: random.Random) -> int:
    """Returns the number of files made. Wipe first (`wipe`): names are unique within a folder, and a
    second run would add versions instead."""
    if not all(email in {r.email for r in residents} for email in LOGINS):
        return 0
    today = timezone.localdate(now)
    ensure_structure(today)
    s = _Seeder(residents, now)
    _seed_common(s)
    _seed_groups(s)
    return File.objects.count()


def _seed_common(s: _Seeder) -> None:
    formand, beboer = s.login("formand@gahk.dk"), s.login("beboer@gahk.dk")
    ak, oel = s.login("ak@gahk.dk"), s.login("oel@gahk.dk")
    timeless = Folder.objects.get(kind=Folder.Kind.TIMELESS, space=Space.COMMON, name=COMMON_TIMELESS)

    # Only admins and Inspektionen make the first level of `Uden årstal`; anyone may fill it.
    guides = s.folder(timeless, "Vejledninger", formand, s.ago(200))
    s.upload(guides, "Husorden.md", _HOUSE_RULES.encode(), formand, s.ago(200))
    s.upload(guides, "Telefonliste.csv", _csv(_PHONE_LIST), formand, s.ago(150))
    s.upload(guides, "Affaldssortering.png", _png("Affaldssortering", "#517544", (1200, 800)), oel, s.ago(90))
    s.upload(guides, "Vaskeriet.pdf", _pdf("Vaskeriet", _LAUNDRY), ak, s.ago(60))
    templates = s.folder(timeless, "Skabeloner", formand, s.ago(190))
    s.upload(templates, "Referatskabelon.docx", _docx("Referat", _MINUTES_TEMPLATE), formand, s.ago(190))
    s.upload(templates, "Budgetskabelon.xlsx", _xlsx(_BUDGET_TEMPLATE), formand, s.ago(185))
    # A plain resident's folder one level down, which they may make.
    expenses = s.folder(templates, "Udlæg", beboer, s.ago(40))
    s.upload(expenses, "Udlægsseddel.pdf", _pdf("Udlægsseddel", _EXPENSE_FORM), beboer, s.ago(40))

    this_year = _year_folder(s.today.year)
    start = date(s.today.year, 1, 1)
    party = s.folder(this_year, "Julefrokost", beboer, s.ago(20, not_before=start))
    s.upload(party, "Indkøbsliste.csv", _csv(_SHOPPING), beboer, s.ago(20, not_before=start))
    s.upload(
        party, "Plakat.png", _png("Julefrokost", "#8a4e70", (900, 1270)), oel, s.ago(12, not_before=start)
    )
    s.upload(
        this_year,
        "Husmøde referat.docx",
        _docx("Husmøde", _HOUSE_MEETING),
        formand,
        s.ago(30, not_before=start),
    )

    # Last year is still writable; the year before is read-only to every resident.
    last_year = s.today.year - 1
    s.upload(
        _year_folder(last_year),
        f"Generalforsamling {last_year}.pdf",
        _pdf(f"Generalforsamling {last_year}", _GENERAL_MEETING),
        formand,
        _noon(date(last_year, 4, 20)),
    )
    s.upload(
        _year_folder(last_year - 1),
        f"Årsberetning {last_year - 1}.pdf",
        _pdf(f"Årsberetning {last_year - 1}", _ANNUAL_REPORT),
        formand,
        _noon(date(last_year - 1, 12, 1)),
    )


def _seed_groups(s: _Seeder) -> None:
    current = period_containing(s.today)
    previous, older = current.previous(), current.previous().previous()
    starts = current.start

    regnskab = s.login("regnskab@gahk.dk")
    if (group := _group_of(regnskab, s.today)) is not None:
        timeless = _group_folder(group, Folder.Kind.TIMELESS)
        s.upload(timeless, "Kontoplan.xlsx", _xlsx(_ACCOUNTS), regnskab, s.ago(300))
        s.upload(timeless, "Sådan bogfører vi.md", _BOOKKEEPING.encode(), regnskab, s.ago(280))
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        s.upload(
            now_folder, f"Budget {current.name}.xlsx", _xlsx(_BUDGET), regnskab, s.ago(25, not_before=starts)
        )
        # Nested, so the breadcrumbs and subtree counts have depth to show.
        receipts = s.path(now_folder, "Bilag/Indkøb", regnskab, s.ago(22, not_before=starts))
        for number, colour in enumerate(["#dd6b43", "#d8a32f", "#315b8d"], start=1):
            s.upload(
                receipts,
                f"Kvittering {number}.jpg",
                _jpg(f"Kvittering {number}", colour, (800, 1100)),
                regnskab,
                s.ago(22 - number * 3, not_before=starts),
            )
        prev_folder = _group_folder(group, Folder.Kind.PERIOD, previous)
        s.upload(
            prev_folder,
            f"Regnskab {previous.name}.pdf",
            _pdf(f"Regnskab {previous.name}", _ACCOUNTS_REPORT),
            regnskab,
            _noon(previous.start + timedelta(days=150)),
        )
        old_folder = _group_folder(group, Folder.Kind.PERIOD, older)
        s.upload(
            old_folder,
            f"Regnskab {older.name}.pdf",
            _pdf(f"Regnskab {older.name}", _ACCOUNTS_REPORT),
            regnskab,
            _noon(older.start + timedelta(days=150)),
        )

    ak = s.login("ak@gahk.dk")
    if (group := _group_of(ak, s.today)) is not None:
        s.upload(
            _group_folder(group, Folder.Kind.TIMELESS), "Værktøjsliste.xlsx", _xlsx(_TOOLS), ak, s.ago(240)
        )
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        s.upload(now_folder, "Arbejdsplan.csv", _csv(_WORK_PLAN), ak, s.ago(18, not_before=starts))
        s.upload(
            now_folder, "AK-møde referat.docx", _docx("AK-møde", _AK_MEETING), ak, s.ago(9, not_before=starts)
        )

    oel = s.login("oel@gahk.dk")
    if (group := _group_of(oel, s.today)) is not None:
        s.upload(
            _group_folder(group, Folder.Kind.TIMELESS),
            "Logo.png",
            _png("Ølkælderen", "#d8a32f", (600, 600)),
            oel,
            s.ago(400),
        )
        now_folder = _group_folder(group, Folder.Kind.PERIOD, current)
        s.upload(now_folder, "Prisliste.csv", _csv(_PRICES), oel, s.ago(15, not_before=starts))
        s.upload(now_folder, "Indkøb.xlsx", _xlsx(_PURCHASES), oel, s.ago(5, not_before=starts))
        s.upload(
            _group_folder(group, Folder.Kind.PERIOD, previous),
            f"Status {previous.name}.pdf",
            _pdf(f"Status {previous.name}", _CELLAR_STATUS),
            oel,
            _noon(previous.start + timedelta(days=170)),
        )

    beboer = s.login("beboer@gahk.dk")
    if (group := _group_of(beboer, s.today)) is not None:
        s.upload(
            _group_folder(group, Folder.Kind.PERIOD, current),
            f"Noter fra {folder_name(group)}.txt",
            _NOTES.encode(),
            beboer,
            s.ago(3, not_before=starts),
        )


def wipe() -> None:
    """Every Drive row, and the bytes they point at. Folders go from the leaves up: a parent is
    PROTECTed by its children."""
    store = services.storage()
    for key in set(FileVersion.objects.values_list("key", flat=True)):
        store.delete(key)
    File.objects.all().delete()
    while Folder.objects.exists():
        Folder.objects.filter(children__isnull=True).delete()


# --- Contents ------------------------------------------------------------------------------------


def _png(title: str, colour: str, size: tuple[int, int]) -> bytes:
    return _image(title, colour, size, "PNG")


def _jpg(title: str, colour: str, size: tuple[int, int]) -> bytes:
    return _image(title, colour, size, "JPEG")


def _image(title: str, colour: str, size: tuple[int, int], kind: str) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, colour)
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 24, size[0] - 24, size[1] - 24), outline="white", width=6)
    draw.text((56, 56), title, fill="white", font_size=min(size) // 10)
    output = BytesIO()
    image.save(output, format=kind)
    return output.getvalue()


def _pdf(title: str, lines: list[str]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_creation_date(datetime(2026, 1, 1, tzinfo=UTC))  # the same bytes every run
    pdf.add_page()
    pdf.set_font("helvetica", "B", 20)
    pdf.cell(text=title, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("helvetica", size=12)
    for line in lines:
        pdf.multi_cell(0, 7, text=line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def _csv(rows: list[list[str]]) -> bytes:
    output = StringIO()
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode()


def _xlsx(rows: list[list[object]]) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    assert sheet is not None  # noqa: S101  # nosec B101 - a new workbook has a sheet
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    book.save(output)
    return output.getvalue()


_DOCX_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    ),
}


def _docx(title: str, paragraphs: list[str]) -> bytes:
    """The smallest Word document Word opens: three parts, one paragraph per line, the title bold."""
    runs = [f'<w:r><w:rPr><w:b/><w:sz w:val="32"/></w:rPr><w:t>{escape(title)}</w:t></w:r>']
    runs += [f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>' for text in paragraphs]
    body = "".join(f"<w:p>{run}</w:p>" for run in runs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in [*_DOCX_PARTS.items(), ("word/document.xml", document)]:
            archive.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), text)
    return output.getvalue()


_HOUSE_RULES = """# Husorden

Kort udgave af husordenen. Den fulde står i vedtægterne.

## Ro

- Der er ro på gangene **kl. 23–7** på hverdage.
- Fester i fællesrummet slutter kl. 2 og meldes på opslagstavlen.

## Køkkenet

1. Vask op efter dig samme dag.
2. Mærk dine ting i køleskabet med navn og dato.
3. Tøm skraldespanden, når den er fuld.

| Hvad | Hvor |
| --- | --- |
| Pap | Containeren i gården |
| Glas | Kælderen ved cykelrummet |
"""

_BOOKKEEPING = """# Sådan bogfører vi

1. Læg bilaget i `Bilag` i den periode, udgiften hører til.
2. Navngiv det med dato og beløb, fx `2026-11-03 Netto 412,50.jpg`.
3. Bogfør i e-conomic samme uge.

Spørgsmål? Skriv til regnskabsgruppen.
"""

_NOTES = """Noter fra gruppemødet

- Fordel opgaverne for resten af perioden
- Bestil nøgler til de nye
- Næste møde om to uger
"""

_PHONE_LIST = [
    ["Navn", "Rolle", "Telefon"],
    ["Vicevært", "Nøgler og reparationer", "33 12 34 56"],
    ["Inspektionen", "Husets drift", "33 12 34 57"],
    ["Vaskeriet", "Fejl på maskinerne", "70 20 30 40"],
]
_SHOPPING = [
    ["Vare", "Antal", "Ansvarlig"],
    ["Sild", "4 glas", "Bente"],
    ["Rugbrød", "6", "Ole"],
    ["Snaps", "2 flasker", "Astrid"],
    ["Risalamande", "30 portioner", "Frederik"],
]
_WORK_PLAN = [
    ["Uge", "Opgave", "Krydser"],
    ["45", "Rens tagrender", "3"],
    ["46", "Mal cykelkælderen", "4"],
    ["47", "Ryd haven for blade", "2"],
    ["48", "Juletræ i gården", "2"],
]
_PRICES = [
    ["Vare", "Pris (kr.)"],
    ["Grøn Tuborg", "7"],
    ["Carlsberg", "7"],
    ["Fadøl", "10"],
    ["Sodavand", "5"],
]
_ACCOUNTS: list[list[object]] = [
    ["Konto", "Navn", "Type"],
    [1000, "Huslejeindtægter", "Indtægt"],
    [1100, "Ølkælderen", "Indtægt"],
    [2000, "Vedligehold", "Udgift"],
    [2100, "Fester", "Udgift"],
    [2200, "Rengøringsartikler", "Udgift"],
]
_BUDGET: list[list[object]] = [
    ["Post", "Budget (kr.)", "Forbrugt (kr.)"],
    ["Vedligehold", 40000, 12450],
    ["Fester", 15000, 6200],
    ["Rengøringsartikler", 8000, 3100],
    ["I alt", "=SUM(B2:B4)", "=SUM(C2:C4)"],
]
_BUDGET_TEMPLATE: list[list[object]] = [["Post", "Budget (kr.)", "Forbrugt (kr.)"], ["", 0, 0]]
_TOOLS: list[list[object]] = [
    ["Værktøj", "Antal", "Placering"],
    ["Boremaskine", 2, "Værkstedet"],
    ["Stige", 3, "Kælderen"],
    ["Malerruller", 12, "Værkstedet"],
]
_PURCHASES: list[list[object]] = [
    ["Dato", "Leverandør", "Beløb (kr.)"],
    ["2026-10-04", "Bryggeriet", 4800],
    ["2026-10-25", "Grossisten", 2150],
    ["2026-11-08", "Bryggeriet", 5100],
]
_LAUNDRY = [
    "Vaskeriet er åbent alle dage kl. 7-22.",
    "Book en tid på tavlen ved døren. En tid er to timer.",
    "Tøm filteret i tørretumbleren efter brug.",
]
_EXPENSE_FORM = ["Navn:", "Værelse:", "Beløb:", "Formål:", "Vedlæg kvitteringen."]
_GENERAL_MEETING = [
    "Dagsorden: valg af dirigent, beretning, regnskab, indkomne forslag, eventuelt.",
    "Beretningen og regnskabet blev godkendt.",
    "Forslaget om nye cykelstativer blev vedtaget.",
]
_ANNUAL_REPORT = [
    "Året bød på en ny tagterrasse og et renoveret fælleskøkken.",
    "Kollegiet havde 61 beboere og fuld belægning hele året.",
]
_ACCOUNTS_REPORT = [
    "Indtægter og udgifter for perioden, afstemt med e-conomic.",
    "Resultat: et lille overskud, som overføres til næste periode.",
]
_CELLAR_STATUS = ["Omsætning i perioden og lagerstatus ved periodens slutning.", "Alle fadøl er talt op."]
_MINUTES_TEMPLATE = ["Dato:", "Til stede:", "Dagsorden:", "1. ", "2. ", "Næste møde:"]
_HOUSE_MEETING = [
    "Til stede: 24 beboere.",
    "Julefrokosten holdes i fællesrummet. Festudvalget står for maden.",
    "Cykelkælderen ryddes; cykler uden navn fjernes efter en måned.",
]
_AK_MEETING = [
    "Gennemgang af arbejdsplanen for resten af perioden.",
    "Tagrenderne er renset. Cykelkælderen males i uge 46.",
]
