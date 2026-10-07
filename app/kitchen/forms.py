import datetime
from typing import Any

from django import forms
from django.utils import timezone

from core.clock import current_datetime
from residents.models import Resident

from .models import KitchenShift


class SpecialShiftForm(forms.Form):
    description = forms.CharField(label="Beskrivelse", max_length=255)
    date = forms.DateField(label="Dato", widget=forms.DateInput(attrs={"type": "date"}))
    start = forms.TimeField(label="Start", widget=forms.TimeInput(attrs={"type": "time"}))
    end = forms.TimeField(label="Slut", widget=forms.TimeInput(attrs={"type": "time"}))
    points_per_spot = forms.IntegerField(label="Point pr. plads", min_value=0, max_value=100, initial=1)
    spots = forms.IntegerField(label="Antal pladser", min_value=1, max_value=50, initial=1)

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if self.errors:
            return data
        starts_at = timezone.make_aware(datetime.datetime.combine(data["date"], data["start"]))
        ends_at = timezone.make_aware(datetime.datetime.combine(data["date"], data["end"]))
        if ends_at <= starts_at:
            raise forms.ValidationError("Vagten skal slutte efter den starter.")
        if starts_at <= current_datetime():
            raise forms.ValidationError("Vagten skal ligge i fremtiden.")
        data["starts_at"], data["ends_at"] = starts_at, ends_at
        return data

    def save(self, created_by: Resident) -> KitchenShift:
        data = self.cleaned_data
        return KitchenShift.objects.create(
            kind=KitchenShift.Kind.SPECIAL,
            date=data["date"],
            starts_at=data["starts_at"],
            ends_at=data["ends_at"],
            spots=data["spots"],
            points_per_spot=data["points_per_spot"],
            description=data["description"],
            created_by=created_by,
        )
