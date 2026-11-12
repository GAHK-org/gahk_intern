"""The Hjemmeside editor's forms.

They only shape what the editor types into the `data` the services take; cms.services does the
cleaning that matters (sanitizing HTML, checking addresses and images) and raises ValidationError
keyed by its own field names, which the views put back on these fields (`SERVICE_FIELDS`).

The one rule kept here is the address picker's, ported from the old Django-admin page form: an
address is composed from a section (a top-level page) and a last segment, so an editor cannot type
`faciliteter-kokken` where `faciliteter/kokken` was meant.
"""

from typing import Any, cast

from django import forms
from django.core.exceptions import ValidationError

from .models import FRONT_PAGE_ID, CmsImage, Page
from .paths import is_reserved, join_path, normalize_segment, split_path, validate_page_path

# The HTML field the body editor (frontend/src/hjemmeside-editor.ts) takes over. Without JavaScript it
# stays a plain textarea of HTML, which saves just as well.
EDITOR_ATTRS = {"rows": 18, "data-hjemmeside-editor": "", "spellcheck": "true"}


def library_choices(current: str) -> list[tuple[str, str]]:
    """("", none) plus every library image; a stored value that is not in the library is kept, so a
    page whose old background has not been converted yet still saves unchanged."""
    choices: list[tuple[str, str]] = [("", "Ingen")]
    choices += [(image.url, str(image)) for image in CmsImage.objects.all() if image.file]
    if current and current not in {value for value, _label in choices}:
        choices.insert(1, (current, f"{current} (nuværende)"))
    return choices


class PageForm(forms.Form):
    header = forms.CharField(label="Titel", max_length=255)
    path_parent = forms.ChoiceField(
        label="Placering",
        required=False,
        help_text="Den sektion siden ligger under. En side under en sektion vises i sektionens "
        "undermenu på gahk.dk.",
    )
    path_segment = forms.CharField(
        label="Adresse",
        max_length=80,
        required=False,
        widget=forms.TextInput(attrs={"autocapitalize": "off", "spellcheck": "false"}),
        help_text="Kun små bogstaver, tal og bindestreg, fx “kokken”. Uden adresse vises siden ikke "
        "på gahk.dk. Ændrer du adressen, sender den gamle videre til den nye.",
    )
    description = forms.CharField(
        label="Beskrivelse",
        max_length=300,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="En sætning eller to til søgemaskiner og delte links. Valgfri.",
    )
    background_image = forms.ChoiceField(label="Baggrundsbillede", required=False)
    body = forms.CharField(label="Indhold", required=False, widget=forms.Textarea(attrs=EDITOR_ATTRS))
    # The page's updated_at when the form was opened, so a save over somebody else's newer edit is
    # caught instead of silently undoing it (PageEditView).
    loaded_at = forms.CharField(required=False, widget=forms.HiddenInput)

    # Where cms.services' ValidationError keys land on this form.
    SERVICE_FIELDS = {"slug": "path_segment"}

    def __init__(self, *args: Any, page: Page | None = None, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        self.page = page
        current_parent, _segment = split_path(page.slug if page else None)
        cast(forms.ChoiceField, self.fields["path_parent"]).choices = self._parent_choices(current_parent)
        cast(forms.ChoiceField, self.fields["background_image"]).choices = library_choices(
            page.background_image if page else ""
        )
        if self.is_front_page:
            # gahk.dk shows the front page at "/" whatever its address says; offering to change it
            # would only invite a meaningless edit.
            del self.fields["path_parent"]
            del self.fields["path_segment"]

    @property
    def is_front_page(self) -> bool:
        return self.page is not None and self.page.pk == FRONT_PAGE_ID

    def _parent_choices(self, current_parent: str) -> list[tuple[str, str]]:
        """Top-level pages, offered as sections. Never the page itself, which would nest it in itself."""
        sections = (
            Page.objects.filter(slug__isnull=False)
            .exclude(slug="")
            .exclude(slug__contains="/")
            .order_by("slug")
        )
        if self.page is not None and self.page.pk:
            sections = sections.exclude(pk=self.page.pk)
        choices: list[tuple[str, str]] = [("", "Øverste niveau")]
        choices += [(page.slug or "", f"Under {page.header} (/{page.slug})") for page in sections]
        if current_parent and current_parent not in {value for value, _label in choices}:
            choices.insert(1, (current_parent, f"Under /{current_parent} (nuværende)"))
        return choices

    def clean(self) -> dict[str, Any]:
        super().clean()
        cleaned = self.cleaned_data
        if self.is_front_page:
            return cleaned
        parent = (cleaned.get("path_parent") or "").strip()
        segment = normalize_segment(cleaned.get("path_segment") or "")
        cleaned["path_segment"] = segment
        if "/" in segment:
            self.add_error(
                "path_segment", "Den sidste del må ikke indeholde “/”. Vælg sektionen under Placering."
            )
            return cleaned
        if parent and not segment:
            self.add_error("path_segment", "Skriv den sidste del af adressen, når siden ligger i en sektion.")
            return cleaned
        composed = join_path(parent, segment)
        if composed:
            try:
                validate_page_path(composed)
            except ValidationError as exc:
                self.add_error("path_segment", exc)
                return cleaned
            clash = Page.objects.filter(slug=composed)
            if self.page is not None and self.page.pk:
                clash = clash.exclude(pk=self.page.pk)
            if other := clash.first():
                self.add_error(
                    "path_segment", f"Adressen /{composed} bruges allerede af siden “{other.header}”."
                )
                return cleaned
        cleaned["slug"] = composed or None
        return cleaned

    def service_data(self) -> dict[str, Any]:
        """What cms.services.save_page takes."""
        data = {
            name: self.cleaned_data.get(name, "")
            for name in ("header", "body", "background_image", "description")
        }
        if not self.is_front_page:
            data["slug"] = self.cleaned_data.get("slug")
        return data

    def preview_data(self) -> dict[str, Any]:
        """The unsaved form as cms.services.render_preview takes it, valid or not."""
        raw = self.data
        slug = ""
        if not self.is_front_page:
            parent = str(raw.get("path_parent") or "").strip()
            segment = normalize_segment(str(raw.get("path_segment") or ""))
            slug = join_path(parent, segment)
            if slug and is_reserved(slug):
                slug = ""
        return {
            "slug": slug or (self.page.slug if self.page else ""),
            "header": raw.get("header", ""),
            "body": raw.get("body", ""),
            "background_image": raw.get("background_image", ""),
            "description": raw.get("description", ""),
        }


class NewsForm(forms.Form):
    title = forms.CharField(label="Titel", max_length=255)
    published_at = forms.DateTimeField(
        label="Udgivet",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"],
        help_text="Nyhederne står nyeste først. Tomt: nu.",
    )
    body = forms.CharField(label="Tekst", required=False, widget=forms.Textarea(attrs=EDITOR_ATTRS))

    SERVICE_FIELDS: dict[str, str] = {}

    def service_data(self) -> dict[str, Any]:
        data = {"title": self.cleaned_data["title"], "body": self.cleaned_data.get("body", "")}
        if self.cleaned_data.get("published_at"):
            data["published_at"] = self.cleaned_data["published_at"]
        return data

    def preview_data(self) -> dict[str, Any]:
        raw = self.data
        return {
            "title": raw.get("title", ""),
            "body": raw.get("body", ""),
            "published_at": str(raw.get("published_at") or "").replace("T", " "),
        }


class EventForm(forms.Form):
    title = forms.CharField(label="Titel", max_length=255)
    starts_on = forms.DateField(
        label="Dato",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        help_text="Begivenheden står på forsiden og under Begivenheder, til dagen er gået.",
    )
    description = forms.CharField(
        label="Beskrivelse", required=False, widget=forms.Textarea(attrs=EDITOR_ATTRS)
    )

    SERVICE_FIELDS: dict[str, str] = {}

    def service_data(self) -> dict[str, Any]:
        return {
            "title": self.cleaned_data["title"],
            "starts_on": self.cleaned_data["starts_on"],
            "description": self.cleaned_data.get("description", ""),
        }

    def preview_data(self) -> dict[str, Any]:
        raw = self.data
        return {
            "title": raw.get("title", ""),
            "description": raw.get("description", ""),
            "starts_on": raw.get("starts_on", ""),
        }


class CaptionForm(forms.Form):
    caption = forms.CharField(
        label="Beskrivelse (alt-tekst)",
        max_length=255,
        required=False,
        help_text="Hvad billedet viser, for dem der ikke kan se det. Bruges når billedet sættes ind i en "
        "tekst; billeder der allerede står i en tekst, beholder deres alt-tekst.",
    )


class SiteSettingsForm(forms.Form):
    default_background = forms.ChoiceField(label="Standardbaggrund", required=False)

    def __init__(self, *args: Any, current: str = "", **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        cast(forms.ChoiceField, self.fields["default_background"]).choices = library_choices(current)


class PageChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj: Page) -> str:
        return f"{obj.header} (/{'' if obj.pk == FRONT_PAGE_ID else obj.slug})"


class RedirectForm(forms.Form):
    old_path = forms.CharField(
        label="Gammel adresse",
        max_length=80,
        widget=forms.TextInput(attrs={"placeholder": "fx gammel-side", "autocapitalize": "off"}),
    )
    page = PageChoiceField(
        label="Sender videre til",
        queryset=Page.objects.exclude(slug__isnull=True).order_by("slug"),
        empty_label="Vælg side",
    )
