"""Page types: fixed pages on gahk.dk with texts of their own.

An ordinary page is a title and one text. A page of a page type has a fixed layout on gahk.dk,
designed in gahk_web's template for the type (render.PAGE_TYPE_TEMPLATES there), and instead of one
text a few named fields that the editors fill in here. The type says which fields there are and
whether each is formatted text (HTML, sanitized and published like a body) or one line of plain text.
gahk_web's template decides where each goes.

A page gets its type from a migration, never from the editor: the layout depends on the address
(the forms link to /optagelse), so a typed page keeps its address and cannot be deleted
(cms.services). A new page type is one entry here, one template in gahk_web, and a migration that
gives a page the type. gahk_web must know the type before intern publishes a page of it; until it
does, it renders the page as an ordinary page.
"""

from dataclasses import dataclass
from enum import StrEnum


class FieldKind(StrEnum):
    HTML = "html"  # the body editor; sanitized like a body (cms.sanitize)
    PLAIN = "plain"  # one line of plain text, escaped wherever it is shown


@dataclass(frozen=True)
class PageField:
    name: str  # the key in Page.fields and in the snapshot: lower-case words joined by "_"
    label: str
    kind: FieldKind
    help_text: str = ""
    max_length: int = 0  # PLAIN only; 0 for HTML, which is bounded like a body


@dataclass(frozen=True)
class PageType:
    name: str  # Page.page_type and the snapshot's page_type
    label: str
    help_text: str
    fields: tuple[PageField, ...]

    def field(self, name: str) -> PageField | None:
        return next((field for field in self.fields if field.name == name), None)


ADMISSIONS = PageType(
    name="admissions",
    label="Optagelse",
    help_text="Siden ser ud som den gamle optagelsesside på gahk.dk. Trinene og knapperne til de to "
    "formularer står fast; teksterne retter du her. Et tomt felt udelader sin del af siden.",
    fields=(
        PageField("intro", "Tekst øverst", FieldKind.HTML, "Indledningen øverst på siden."),
        PageField(
            "criteria",
            "Boksen",
            FieldKind.HTML,
            "Kriterier, husleje, ledige værelser og sprog. Vises på /optagelse og ved siden af "
            "formularen på /optagelse/ansoeg. En overskrift starter et afsnit og bliver grøn, "
            "kursiv bliver grå.",
        ),
        PageField("quote", "Citat", FieldKind.PLAIN, "Citatet under boksen, med «» om det.", 1000),
        PageField("quote_by", "Citat af", FieldKind.PLAIN, "Hvem der sagde det.", 255),
    ),
)

PAGE_TYPES: dict[str, PageType] = {page_type.name: page_type for page_type in (ADMISSIONS,)}


def page_type_of(page: object) -> PageType | None:
    """The page type of a Page (or PageVersion-like object with page_type), or None for an ordinary page."""
    return PAGE_TYPES.get(str(getattr(page, "page_type", "") or ""))


def field_label(name: str) -> str:
    """The Danish label of a field name in any page type (the name itself if no type has it)."""
    for page_type in PAGE_TYPES.values():
        if field := page_type.field(name):
            return field.label
    return name


def html_fields(page: object) -> list[str]:
    """A typed page's HTML texts (where it may use library images); [] for an ordinary page."""
    page_type = page_type_of(page)
    stored = getattr(page, "fields", None)
    if page_type is None or not isinstance(stored, dict):
        return []
    return [str(stored.get(field.name) or "") for field in page_type.fields if field.kind is FieldKind.HTML]
