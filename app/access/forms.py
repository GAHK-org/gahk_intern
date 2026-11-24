"""Adding a member of the kitchen staff."""

from django import forms

from residents.models import Account


class KitchenStaffForm(forms.Form):
    first_name = forms.CharField(label="Fornavn", max_length=100)
    last_name = forms.CharField(label="Efternavn", max_length=100, required=False)
    email = forms.EmailField(label="E-mail")

    def clean_email(self) -> str:
        email = Account.objects.normalize_email(self.cleaned_data["email"])
        if Account.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Der findes allerede en konto med den e-mail.")
        return email
