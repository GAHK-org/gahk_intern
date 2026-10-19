"""Admin forms for the ØK screen. Thresholds are stored in øre but entered/shown in kroner."""

from decimal import Decimal

from django import forms

from .models import InterestPolicy, PurchasePolicy, Warning


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


class PurchasePolicyForm(forms.ModelForm):
    """The credit limit shown on the ØK admin screen. Entered in kroner as a negative number, e.g.
    -100 means "cannot buy once you owe more than 100 kr"."""

    block_below_kr = forms.DecimalField(
        label="Kan ikke handle under (kr)",
        max_digits=8,
        decimal_places=2,
        help_text="Negativt tal, fx -100. Gælder kun når spærringen er slået til.",
    )

    class Meta:
        model = PurchasePolicy
        fields = ["active"]
        labels = {"active": "Spærring aktiveret"}

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["block_below_kr"].initial = Decimal(self.instance.block_below_ore) / 100

    def save(self, commit: bool = True) -> PurchasePolicy:
        obj = super().save(commit=False)
        obj.block_below_ore = int((self.cleaned_data["block_below_kr"] * 100).to_integral_value())
        if commit:
            obj.save()
        return obj
