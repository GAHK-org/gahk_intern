"""Importing a printed rota: parse it, match its areas and names, and turn it into assignments.

Two inputs are understood:

  * the text `pdftotext -layout` makes of "Rengøringslister E26.pdf": an area header line, a
    `Dato  Hvem  Stjerne  Taget  Kommentar  Godkendt` line, then `dd/mm/yyyy  Name  [*]` rows;
  * CSV with the columns område, dato, navn, stjerne (`;` or `,`; dates dd/mm/yyyy or yyyy-mm-dd).

The list names people the way the house does — "Wilma", "William P", "Tobias Hermann" — so names are
matched against residents (`match_name`), and whatever is ambiguous or unknown is settled by hand in
the import's second step before anything is saved. Area headers that match no area are skipped and
reported: the same PDF also carries the køkkenrengøringer, which are out of scope here.
"""

import csv
import datetime
import io
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from residents.models import Resident

from .models import CleaningArea, CleaningAssignment

DATE_ROW = re.compile(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b(.*)$")
COLUMNS = re.compile(r"\s{2,}")
STAR_WORDS = {"*", "x", "ja", "1", "true", "stjerne"}
PDF_TIMEOUT_S = 20


@dataclass
class Row:
    header: str  # the area as the list names it
    date: datetime.date
    name: str  # "" = nobody on the list that week
    star: bool


@dataclass
class Parsed:
    rows: list[Row] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def norm(text: str) -> str:
    """Case-, accent-width- and whitespace-insensitive form used for every comparison here."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def parse_layout_text(text: str) -> Parsed:
    out = Parsed()
    header = ""
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or norm(line).startswith("dato "):
            continue
        m = DATE_ROW.match(line)
        if not m:
            header = line.strip()
            continue
        if not header:
            out.errors.append(f"Linje {n}: en dato før første område-overskrift.")
            continue
        try:
            day = datetime.date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            out.errors.append(f"Linje {n}: ugyldig dato.")
            continue
        cells = [c.strip() for c in COLUMNS.split(m[4].strip()) if c.strip()]
        star = any(c == "*" for c in cells) or bool(cells and cells[0].endswith("*"))
        names = [c for c in cells if c != "*"]
        out.rows.append(Row(header, day, names[0].rstrip("*").strip() if names else "", star))
    return out


def parse_csv(text: str) -> Parsed:
    out = Parsed()
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    for n, cells in enumerate(reader, start=1):
        cells = [c.strip() for c in cells]
        if not any(cells) or (n == 1 and norm(cells[0]) in {"område", "omraade", "area"}):
            continue
        if len(cells) < 3:
            out.errors.append(f"Linje {n}: forventede område, dato, navn[, stjerne].")
            continue
        area, raw_date, name = cells[:3]
        day = _parse_date(raw_date)
        if day is None:
            out.errors.append(f"Linje {n}: ugyldig dato {raw_date!r}.")
            continue
        star = len(cells) > 3 and norm(cells[3]) in STAR_WORDS
        out.rows.append(Row(area, day, name, star))
    return out


def _parse_date(raw: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        pass
    m = DATE_ROW.match(raw)
    if not m:
        return None
    try:
        return datetime.date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None


def parse(text: str) -> Parsed:
    """Either format. In the PDF's layout the rows START with a date; in CSV they start with the
    area. (Counting commas would not do: "Stuen, Gang, Toilet" is a PDF header.)"""
    if any(DATE_ROW.match(line) for line in text.splitlines()):
        return parse_layout_text(text)
    return parse_csv(text)


def pdftotext_available() -> bool:
    return shutil.which("pdftotext") is not None


def pdf_to_text(data: bytes) -> str:
    """`pdftotext -layout` on an uploaded PDF (managers only; fixed argv, no shell, time-boxed)."""
    exe = shutil.which("pdftotext")
    if exe is None:
        raise RuntimeError("pdftotext er ikke installeret på serveren.")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(data)
        tmp.flush()
        done = subprocess.run(  # noqa: S603 — fixed argv, our own temp file
            [exe, "-layout", tmp.name, "-"], capture_output=True, timeout=PDF_TIMEOUT_S, check=True
        )
    return done.stdout.decode("utf-8", errors="replace")


# ---- matching ----


def match_areas(headers: Iterable[str], areas: Sequence[CleaningArea]) -> dict[str, CleaningArea | None]:
    """Each distinct header → the area whose name or an import alias it is (None = skipped)."""
    lookup: dict[str, CleaningArea] = {}
    for area in areas:
        for label in [area.name, *area.aliases()]:
            lookup.setdefault(norm(label), area)
    return {h: lookup.get(norm(h)) for h in dict.fromkeys(headers)}


@dataclass
class NameMatch:
    name: str
    resident: Resident | None  # the one match, if there is exactly one
    candidates: list[Resident]  # every plausible resident when it is ambiguous

    @property
    def state(self) -> str:
        if self.resident:
            return "match"
        return "flere" if self.candidates else "ingen"


def match_name(name: str, people: Sequence[Resident]) -> NameMatch:
    """Match a name as the list writes it against residents, from strict to loose:

      1. the full name ("Tobias Hermann" = Tobias Hermann);
      2. the whole thing as a first name ("Anton Claes" with first_name "Anton Claes");
      3. first name + the start of the last name ("William P", "Gustav Skovsager", "Michelle Kramer");
      4. the first name alone, if exactly one resident has it ("Wilma").

    The first rule that finds exactly one resident wins. If none does, the residents the loosest
    rule found are the candidates to choose between.
    """
    n = norm(name)
    tokens = n.split()
    if not tokens:
        return NameMatch(name, None, [])
    first, rest = tokens[0], " ".join(tokens[1:])

    def firsts(p: Resident) -> list[str]:
        return norm(p.first_name).split()

    rules = [
        [p for p in people if norm(p.full_name) == n],
        [p for p in people if norm(p.first_name) == n],
        [p for p in people if firsts(p)[:1] == [first] and rest and _last_starts(p, rest)],
        [p for p in people if firsts(p)[:1] == [first] and not rest],
    ]
    for found in rules:
        if len(found) == 1:
            return NameMatch(name, found[0], [])
    loose = [p for p in people if firsts(p)[:1] == [first]]
    return NameMatch(name, None, loose)


def _last_starts(p: Resident, rest: str) -> bool:
    """`rest` is the start of the last name, or of one of its words ("P" for "Pedersen", "Kramer"
    for "Kramer Nielsen", "Skovsager" for "Bach Skovsager")."""
    last = norm(p.last_name)
    words = last.split()
    return last.startswith(rest) or any(" ".join(words[i:]).startswith(rest) for i in range(len(words)))


def build_assignments(
    rows: Iterable[Row], areas: dict[str, CleaningArea | None], residents: dict[str, Resident | None]
) -> list[CleaningAssignment]:
    """Unsaved assignments for every row whose area is known; `residents` maps each name to who it
    was resolved to (None = "Ingen på listen")."""
    out = []
    for row in rows:
        area = areas.get(row.header)
        if area is None:
            continue
        out.append(
            CleaningAssignment(
                area=area,
                date=row.date,
                resident=residents.get(row.name) if row.name else None,
                is_star_week=row.star,
            )
        )
    return out
