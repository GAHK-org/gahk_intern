"""Forms for planning rotas and editing single assignments (Inspektionen only)."""

import datetime
import subprocess
from typing import Any, cast

from django import forms
from django.db.models import Q
from django.forms.boundfield import BoundField

from residents.models import Resident

from . import importing
from .models import CleaningArea, CleaningAssignment, Rota
from .services import rota_residents, swap_candidates

# How many people an area's rotation can have. The wiki's areas are shared by 3-4 alumner; two spare
# slots cover an odd semester without the form growing a JavaScript "add row" button.
ROTATION_SLOTS = 6

SUNDAY = 6  # date.weekday()


class ResidentChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj: Resident) -> str:
        return obj.full_name


class DateInput(forms.DateInput):
    input_type = "date"


class RotaForm(forms.Form):
    """Name, period and star cadence, plus for every active area an ordered list of residents."""

    name = forms.CharField(label="Navn", max_length=50, help_text='Fx "E26" eller "F27".')
    start_date = forms.DateField(label="Første søndag", widget=DateInput)
    weeks = forms.IntegerField(label="Antal uger", min_value=1, max_value=60, initial=26)
    star_every = forms.IntegerField(
        label="(*)-uge hver",
        min_value=0,
        max_value=52,
        initial=5,
        help_text="Hver 5. søndag i E26. 0 = ingen (*)-uger.",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.areas = list(CleaningArea.objects.filter(is_active=True, kind=CleaningArea.Kind.SOENDAG))
        people = rota_residents()
        for area in self.areas:
            for slot in range(ROTATION_SLOTS):
                self.fields[self._slot(area, slot)] = ResidentChoiceField(
                    queryset=people, required=False, empty_label="—", label=f"{area.name} {slot + 1}"
                )

    @staticmethod
    def _slot(area: CleaningArea, slot: int) -> str:
        return f"area_{area.pk}_{slot}"

    def area_rows(self) -> list[tuple[CleaningArea, list[BoundField]]]:
        return [(area, [self[self._slot(area, s)] for s in range(ROTATION_SLOTS)]) for area in self.areas]

    def clean_name(self) -> str:
        name = self.cleaned_data["name"].strip()
        if Rota.objects.filter(name__iexact=name).exists():
            raise forms.ValidationError("Der findes allerede en liste med det navn.")
        return name

    def clean_start_date(self) -> datetime.date:
        day: datetime.date = self.cleaned_data["start_date"]
        if day.weekday() != SUNDAY:
            raise forms.ValidationError("Vælg en søndag.")
        return day

    def rotations(self) -> dict[CleaningArea, list[Resident | None]]:
        """Each area with at least one resident, mapped to its residents in rotation order."""
        out: dict[CleaningArea, list[Resident | None]] = {}
        for area in self.areas:
            people = [self.cleaned_data.get(self._slot(area, s)) for s in range(ROTATION_SLOTS)]
            chosen = [p for p in people if p]
            if chosen:
                out[area] = list(chosen)
        return out

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if not self.errors and not self.rotations():
            raise forms.ValidationError("Vælg mindst én alumne på mindst ét område.")
        return cleaned


class AssignmentForm(forms.ModelForm):
    clear_sign_off = forms.BooleanField(label="Fjern afkrydsningen", required=False)

    class Meta:
        model = CleaningAssignment
        fields = ["resident", "is_star_week", "comment"]
        field_classes = {"resident": ResidentChoiceField}

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        resident = cast(ResidentChoiceField, self.fields["resident"])
        # Today's residents, plus whoever has it now (who may have moved out since).
        current = self.instance.resident_id
        resident.queryset = Resident.objects.filter(Q(pk__in=rota_residents().values("pk")) | Q(pk=current))
        resident.empty_label = "Ingen på listen"
        if not self.instance.signed_off_at:
            del self.fields["clear_sign_off"]


class ReplaceForm(forms.Form):
    """Give one resident's remaining Sundays on a rota to someone else (moving out)."""

    old = ResidentChoiceField(label="Erstat", queryset=Resident.objects.none())
    new = ResidentChoiceField(
        label="med", queryset=Resident.objects.none(), required=False, empty_label="Ingen på listen"
    )
    from_date = forms.DateField(label="fra og med", widget=DateInput)

    def __init__(self, rota: Rota, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        old = cast(ResidentChoiceField, self.fields["old"])
        new = cast(ResidentChoiceField, self.fields["new"])
        old.queryset = Resident.objects.filter(cleaning_assignments__rota=rota).distinct()
        new.queryset = rota_residents()


class SwapForm(forms.Form):
    """An alumne asks another to take one of their Sundays."""

    recipient = ResidentChoiceField(
        label="Hvem", queryset=Resident.objects.none(), empty_label="— vælg alumne —"
    )
    message = forms.CharField(
        label="Besked",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Valgfri"}),
    )

    def __init__(self, me: Resident, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        cast(ResidentChoiceField, self.fields["recipient"]).queryset = swap_candidates(me)


# ---- import ----

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class ImportForm(forms.Form):
    """Step 1: the printed rota, pasted or uploaded."""

    name = forms.CharField(label="Navn", max_length=50, help_text='Fx "E26".')
    text = forms.CharField(
        label="Indsæt listen",
        required=False,
        widget=forms.Textarea(attrs={"rows": 12, "class": "font-mono text-sm"}),
        help_text="Teksten fra PDF'en (pdftotext -layout) eller CSV: område;dato;navn;stjerne.",
    )
    file = forms.FileField(label="…eller upload en fil", required=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.fields["file"].help_text = (
            "PDF, CSV eller tekst."
            if importing.pdftotext_available()
            else "CSV eller tekst (PDF kræver pdftotext på serveren)."
        )

    clean_name = RotaForm.clean_name

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        upload = cleaned.get("file")
        text = (cleaned.get("text") or "").strip()
        if upload:
            if upload.size > MAX_UPLOAD_BYTES:
                raise forms.ValidationError("Filen er for stor (højst 5 MB).")
            data = upload.read()
            if upload.name.lower().endswith(".pdf"):
                try:
                    text = importing.pdf_to_text(data)
                except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    raise forms.ValidationError(f"PDF'en kunne ikke læses: {exc}") from exc
            else:
                text = _decode(data)
        if not text:
            raise forms.ValidationError("Indsæt listen eller upload en fil.")
        cleaned["source"] = text
        return cleaned


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")  # an Excel CSV saved on Windows


INGEN = "ingen"


class MatchForm(forms.Form):
    """Step 2: one dropdown per distinct name on the list, pre-set when it matched one resident."""

    def __init__(
        self, matches: list[importing.NameMatch], people: list[Resident], *args: object, **kwargs: object
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.matches = matches
        everyone = [(str(p.pk), p.full_name) for p in people]
        for i, m in enumerate(matches):
            likely = [(str(p.pk), p.full_name) for p in m.candidates]
            choices: list[tuple[str, object]] = [("", "— vælg —"), (INGEN, "Ingen på listen")]
            if likely:
                choices.append(("Mulige", likely))
            choices.append(("Alle beboere", everyone))
            self.fields[f"navn_{i}"] = forms.ChoiceField(
                label=m.name,
                choices=choices,
                initial=str(m.resident.pk) if m.resident else "",
                error_messages={"required": "Vælg hvem det er (eller Ingen)."},
            )
        self.people = {str(p.pk): p for p in people}

    def rows(self) -> list[tuple[importing.NameMatch, BoundField]]:
        return [(m, self[f"navn_{i}"]) for i, m in enumerate(self.matches)]

    def resolved(self) -> dict[str, Resident | None]:
        return {
            m.name: None
            if self.cleaned_data[f"navn_{i}"] == INGEN
            else self.people[self.cleaned_data[f"navn_{i}"]]
            for i, m in enumerate(self.matches)
        }
