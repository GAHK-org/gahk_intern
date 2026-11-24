import re
import uuid
from decimal import Decimal, InvalidOperation

from django import forms

from core.models import Workgroup
from residents.models import Person

from .models import CreditAccount

_AMOUNT = re.compile(r"^-?\d{1,9}([.,]\d{1,2})?$")


def parse_kr(text: str) -> int:
    """'12,50' / '12.5' / '1.234,56' → øre. Raises ValueError on anything else."""
    cleaned = text.strip().replace(" ", "").replace("kr", "")
    if "," in cleaned and "." in cleaned:  # 1.234,56: the dot groups thousands
        cleaned = cleaned.replace(".", "")
    if not _AMOUNT.match(cleaned):
        raise ValueError(text)
    try:
        return int((Decimal(cleaned.replace(",", ".")) * 100).to_integral_value())
    except InvalidOperation as exc:
        raise ValueError(text) from exc


class KrField(forms.CharField):
    """Kroner as typed in Danish, cleaned to whole øre."""

    def __init__(self, *, positive: bool = True, **kwargs: object) -> None:
        self.positive = positive
        kwargs.setdefault("max_length", 20)
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.widget.attrs.update({"inputmode": "decimal", "autocomplete": "off", "placeholder": "0,00"})

    def clean(self, value: str) -> int | None:
        text = super().clean(value)
        if not text and not self.required:
            return None
        try:
            ore = parse_kr(text)
        except ValueError as exc:
            raise forms.ValidationError("Skriv et beløb i kroner, fx 12,50.") from exc
        if self.positive and ore <= 0:
            raise forms.ValidationError("Beløbet skal være større end 0.")
        return ore


class TokenForm(forms.Form):
    """Every form that moves money carries the identity of its send (credit.ledger's dedupe token)."""

    token = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        if not self.is_bound:
            self.initial.setdefault("token", uuid.uuid4())


def _label(account: CreditAccount) -> str:
    return f"{account.holder.name} · {account.name} ({account.book.name})"


class TransferForm(TokenForm):
    sender = forms.ModelChoiceField(queryset=CreditAccount.objects.none(), label="Fra")
    receiver = forms.ModelChoiceField(queryset=CreditAccount.objects.none(), label="Til")
    amount = KrField(label="Beløb (kr)")
    sender_text = forms.CharField(label="Din tekst", max_length=140, required=False)
    receiver_text = forms.CharField(label="Modtagerens tekst", max_length=140, required=False)

    def __init__(self, *args: object, senders: object, receivers: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.fields["sender"].queryset = senders  # type: ignore[attr-defined]
        self.fields["receiver"].queryset = receivers  # type: ignore[attr-defined]
        self.fields["sender"].label_from_instance = _label  # type: ignore[attr-defined]
        self.fields["receiver"].label_from_instance = _label  # type: ignore[attr-defined]

    def clean(self) -> dict[str, object]:
        data = super().clean() or {}
        if data.get("sender") is not None and data.get("sender") == data.get("receiver"):
            raise forms.ValidationError("Vælg to forskellige konti.")
        return data


class ChargeForm(TokenForm):
    account = forms.ModelChoiceField(queryset=CreditAccount.objects.none(), label="Fra konto")
    into = forms.ModelChoiceField(queryset=CreditAccount.objects.none(), label="Til")
    amount = KrField(label="Beløb (kr)")
    text = forms.CharField(label="Tekst på kontoen", max_length=140)

    def __init__(self, *args: object, accounts: object, into: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = accounts  # type: ignore[attr-defined]
        self.fields["into"].queryset = into  # type: ignore[attr-defined]
        self.fields["account"].label_from_instance = _label  # type: ignore[attr-defined]
        self.fields["into"].label_from_instance = _label  # type: ignore[attr-defined]


class OpenAccountForm(forms.Form):
    person = forms.ModelChoiceField(queryset=Person.objects.none(), required=False, label="Person")
    workgroup = forms.ModelChoiceField(
        queryset=Workgroup.objects.order_by("name"), required=False, label="Embedsgruppe"
    )
    name = forms.CharField(label="Kontoens navn", max_length=100)
    credit_limit = KrField(label="Kreditgrænse (kr, 0 eller negativ)", positive=False, required=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.fields["person"].queryset = Person.objects.filter(account__isnull=False).order_by(  # type: ignore[attr-defined]
            "first_name", "last_name"
        )

    def clean(self) -> dict[str, object]:
        data = super().clean() or {}
        if bool(data.get("person")) == bool(data.get("workgroup")):
            raise forms.ValidationError("Vælg enten en person eller en embedsgruppe.")
        return data

    def clean_credit_limit(self) -> int | None:
        value = self.cleaned_data.get("credit_limit")
        if value is not None and value > 0:
            raise forms.ValidationError("En kreditgrænse er 0 eller negativ.")
        return value


class LimitForm(forms.Form):
    credit_limit = KrField(label="Kreditgrænse (kr)", positive=False)

    def clean_credit_limit(self) -> int:
        value = self.cleaned_data["credit_limit"]
        if value > 0:
            raise forms.ValidationError("En kreditgrænse er 0 eller negativ.")
        return value


class BookForm(forms.Form):
    name = forms.CharField(label="Navn", max_length=100)
    workgroup = forms.ModelChoiceField(
        queryset=Workgroup.objects.order_by("name"), label="Ansvarlig embedsgruppe"
    )
    bank_name = forms.CharField(label="Bankkontoens navn", max_length=100, initial="Bank")
    bank_reference = forms.CharField(label="Reg.nr. og kontonr.", max_length=40)
    default_credit_limit = KrField(label="Kreditgrænse for nye konti (kr)", positive=False, initial="0,00")
    is_main = forms.BooleanField(label="Hovedbog (alle får en generel konto her)", required=False)
