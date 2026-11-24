"""Giving a role by hand, and adding a member of the kitchen staff."""

import datetime

from django import forms

from core.clock import current_date
from residents.models import Account

from .policy import ADMINISTRATOR, GROUPS, ROLE_LABELS

# What a grant may give here: an embedsgruppe's role, for someone the list says nothing or the wrong
# thing about. Not administrator (the role page, with its two-person rule), not the kitchen's (an
# account of the kitchen staff), not a stage (the list's months decide those).
GRANTABLE = sorted(((role, ROLE_LABELS[role]) for role in GROUPS - {ADMINISTRATOR}), key=lambda c: c[1])


class GrantForm(forms.Form):
    role = forms.ChoiceField(label="Rolle", choices=GRANTABLE)
    reason = forms.CharField(label="Hvorfor", max_length=200)
    starts_on = forms.DateField(label="Fra", widget=forms.DateInput(attrs={"type": "date"}))
    ends_on = forms.DateField(
        label="Til og med",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Tom: indtil den afsluttes.",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.fields["starts_on"].initial = current_date()

    def clean(self) -> dict[str, object]:
        data = super().clean() or {}
        starts, ends = data.get("starts_on"), data.get("ends_on")
        if isinstance(starts, datetime.date) and isinstance(ends, datetime.date) and ends < starts:
            self.add_error("ends_on", "Slutter før den begynder.")
        return data


class KitchenStaffForm(forms.Form):
    first_name = forms.CharField(label="Fornavn", max_length=100)
    last_name = forms.CharField(label="Efternavn", max_length=100, required=False)
    email = forms.EmailField(label="E-mail")

    def clean_email(self) -> str:
        email = Account.objects.normalize_email(self.cleaned_data["email"])
        if Account.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Der findes allerede en konto med den e-mail.")
        return email
