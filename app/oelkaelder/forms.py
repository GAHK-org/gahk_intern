"""Admin forms for the ØK screen. Thresholds are stored in øre but entered/shown in kroner."""

from decimal import Decimal

from django import forms

from .models import InterestPolicy, Shopper, Warning


class WarningForm(forms.ModelForm):
    threshold_kr = forms.DecimalField(label="Beløbsgrænse (kr)", max_digits=8, decimal_places=2)

    class Meta:
        model = Warning
        fields = ["message", "active"]
        labels = {"message": "Besked", "active": "Aktiveret"}
        widgets = {"message": forms.Textarea(attrs={"rows": 14})}

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["threshold_kr"].initial = Decimal(self.instance.threshold_ore) / 100

    def save(self, commit: bool = True) -> Warning:
        obj = super().save(commit=False)
        obj.threshold_ore = int((self.cleaned_data["threshold_kr"] * 100).to_integral_value())
        if commit:
            obj.save()
        return obj


class InterestPolicyForm(forms.ModelForm):
    threshold_kr = forms.DecimalField(label="Gældsgrænse (kr)", max_digits=8, decimal_places=2)

    class Meta:
        model = InterestPolicy
        fields = ["active", "rate_percent"]
        labels = {"active": "Rente aktiveret", "rate_percent": "Rente pr. måned (%)"}

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["threshold_kr"].initial = Decimal(self.instance.threshold_ore) / 100

    def save(self, commit: bool = True) -> InterestPolicy:
        obj = super().save(commit=False)
        obj.threshold_ore = int((self.cleaned_data["threshold_kr"] * 100).to_integral_value())
        if commit:
            obj.save()
        return obj


class _ShopperChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj: Shopper) -> str:
        return obj.resident.full_name


class ExemptionForm(forms.Form):
    """An ølkælder account freed of the monthly interest for a while (or until ended)."""

    shopper = _ShopperChoice(
        queryset=Shopper.objects.select_related("resident").order_by(
            "resident__first_name", "resident__last_name"
        ),
        label="Alumne",
    )
    reason = forms.CharField(label="Begrundelse", max_length=255)
    starts_on = forms.DateField(label="Fra", widget=forms.DateInput(attrs={"type": "date"}))
    ends_on = forms.DateField(
        label="Til og med",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Tom = indtil den afsluttes.",
    )
