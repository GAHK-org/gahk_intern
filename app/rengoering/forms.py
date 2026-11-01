"""Forms for planning rotas and editing single assignments (Inspektionen only)."""

import datetime
from typing import Any, cast

from django import forms
from django.db.models import Q
from django.forms.boundfield import BoundField

from residents.models import Resident

from .models import CleaningArea, CleaningAssignment, Rota
from .services import rota_residents

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
