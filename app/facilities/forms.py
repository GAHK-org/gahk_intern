"""The booking form and the managers' facility form."""

import datetime

from django import forms

from core.clock import current_datetime

from .models import MAX_BOOKING_LENGTH, Booking, Facility

INPUT_FORMATS = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"]


class LocalDateTimeInput(forms.DateTimeInput):
    """A native datetime-local picker, rendered in the one shape it pre-fills ("YYYY-MM-DDTHH:MM").
    The same widget as begivenheder's form (events.forms), which this app does not import."""

    input_type = "datetime-local"

    def __init__(self, **kwargs: object) -> None:
        super().__init__(format="%Y-%m-%dT%H:%M", **kwargs)  # type: ignore[arg-type]


class BookingForm(forms.ModelForm):
    starts_at = forms.DateTimeField(label="Fra", widget=LocalDateTimeInput(), input_formats=INPUT_FORMATS)
    ends_at = forms.DateTimeField(label="Til", widget=LocalDateTimeInput(), input_formats=INPUT_FORMATS)

    class Meta:
        model = Booking
        fields = ["starts_at", "ends_at", "purpose"]
        labels = {"purpose": "Hvad skal den bruges til?"}
        help_texts = {"purpose": "Kun du og Inspektionen kan se det. Andre kan se, hvem der har booket."}

    def __init__(self, *args: object, facility: Facility, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.facility = facility
        self.fields["purpose"].required = True

    def clean_starts_at(self) -> datetime.datetime:
        start: datetime.datetime = self.cleaned_data["starts_at"]
        if start < current_datetime():
            raise forms.ValidationError("Du kan ikke booke i fortiden.")
        return start

    def clean(self) -> dict:
        cleaned = super().clean() or self.cleaned_data
        start, end = cleaned.get("starts_at"), cleaned.get("ends_at")
        if start and end:
            if end <= start:
                self.add_error("ends_at", "Sluttidspunktet skal ligge efter starten.")
            elif end - start > MAX_BOOKING_LENGTH:
                self.add_error("ends_at", f"Du kan højst booke {MAX_BOOKING_LENGTH.days} dage ad gangen.")
        return cleaned


class FacilityForm(forms.ModelForm):
    class Meta:
        model = Facility
        fields = ["name", "description", "capacity", "requires_approval", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 5})}
        help_texts = {"is_active": "Slå fra for at stoppe nye bookinger. Gamle bookinger bliver stående."}


class DecisionNoteForm(forms.Form):
    """The optional reason a manager gives when rejecting or cancelling somebody's booking."""

    note = forms.CharField(max_length=200, required=False, label="Begrundelse")
