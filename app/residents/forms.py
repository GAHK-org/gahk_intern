import unicodedata
from collections.abc import Iterator

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm
from django.core.files.uploadedfile import UploadedFile

from core.uploads import validate_image_upload

from .models import AlumneregningSettings, Resident


class LocalDateInput(forms.DateInput):
    """A native date picker that actually pre-fills.

    `type=date` only accepts a value shaped exactly "YYYY-MM-DD". Under LANGUAGE_CODE="da" Django
    renders dates as "17.05.2000", which the browser silently discards, so an edit form comes up
    blank and quietly wipes the date on save. Same trap — and same fix — as events'
    LocalDateTimeInput.
    """

    input_type = "date"

    def __init__(self, **kwargs: object) -> None:
        super().__init__(format="%Y-%m-%d", **kwargs)  # type: ignore[arg-type]


class EmailAuthenticationForm(AuthenticationForm):
    """Login form relabelled for email (the resident USERNAME_FIELD is `email`)."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "E-mail"
        # An e-mail field, not Django's plain text one: phones show the @ keyboard and leave the
        # address alone. In an installed PWA, where autofill often offers nothing, autocorrect could
        # turn a typed "o" into "ø" and the login failed with the right password.
        widget = self.fields["username"].widget
        widget.input_type = "email"
        widget.attrs.update(
            {
                "autofocus": True,
                "autocomplete": "email",
                "autocapitalize": "none",
                "autocorrect": "off",
                "spellcheck": "false",
            }
        )


class ResidentEditForm(forms.ModelForm):
    """Indstilling edits a resident's core data (everything in the alumneliste that is not per-month:
    room/embedsgruppe/rengøring live on the monthly Residency and are edited on the next-month list)."""

    class Meta:
        model = Resident
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "birthday",
            "study",
            "move_in_date",
            "move_out_date",
            "sponsor",
            "fylgje_raw",
        ]
        labels = {
            "first_name": "Fornavn",
            "last_name": "Efternavn",
            "email": "E-mail",
            "phone": "Telefon",
            "birthday": "Fødselsdag",
            "study": "Studie",
            "move_in_date": "Indflyttet",
            "move_out_date": "Fraflyttet",
            "sponsor": "Fylgje (fadder)",
            "fylgje_raw": "Fylgje (fritekst, hvis ukendt)",
        }
        widgets = {
            "birthday": LocalDateInput(),
            "move_in_date": LocalDateInput(),
            "move_out_date": LocalDateInput(),
        }

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        # Fylgje picker: any other resident, by name; not required.
        sponsor = self.fields["sponsor"]
        if isinstance(sponsor, forms.ModelChoiceField):
            sponsor.queryset = Resident.objects.exclude(pk=self.instance.pk).order_by(
                "first_name", "last_name"
            )
        sponsor.required = False


class ProfileEditForm(forms.ModelForm):
    """A resident edits their own public profile (picture, bio, social links)."""

    class Meta:
        model = Resident
        fields = ["profile_picture", "bio", "facebook_link", "instagram_handle"]
        labels = {
            "profile_picture": "Profilbillede",
            "bio": "Kort bio",
            "facebook_link": "Facebook-link",
            "instagram_handle": "Instagram-brugernavn",
        }
        widgets = {
            "profile_picture": forms.ClearableFileInput(),
            "bio": forms.Textarea(attrs={"rows": 4, "maxlength": 500}),
            "facebook_link": forms.TextInput(attrs={"placeholder": "https://www.facebook.com/..."}),
            "instagram_handle": forms.TextInput(attrs={"placeholder": "@brugernavn"}),
        }
        help_texts = {
            "bio": "Maks. 500 tegn.",
            "facebook_link": "Fuld URL til din Facebook-profil.",
            "instagram_handle": "Dit Instagram-brugernavn (med eller uden @).",
        }

    def clean_profile_picture(self) -> object:
        upload = self.cleaned_data.get("profile_picture")
        if isinstance(upload, UploadedFile):
            return validate_image_upload(upload, settings.PROFILE_PICTURE_MAX_MB)
        return upload


class EconomicAccountNumberForm(forms.ModelForm):
    """Regnskab sets or clears a resident's e-conomic chart-of-accounts accountNumber (alumneregning
    automation, F-010 regnskab role)."""

    class Meta:
        model = Resident
        fields = ["economic_account_number"]
        labels = {"economic_account_number": "E-conomic kontonr."}


class AlumneregningSettingsForm(forms.ModelForm):
    """Regnskab edits the alumneregning mail body and følgebrev PDF in the UI (F-010)."""

    class Meta:
        model = AlumneregningSettings
        fields = ["message_template", "followup_pdf"]
        labels = {
            "message_template": "Besked",
            "followup_pdf": "Følgebrev (PDF)",
        }
        widgets = {
            "message_template": forms.Textarea(attrs={"rows": 16}),
            "followup_pdf": forms.ClearableFileInput(attrs={"accept": "application/pdf"}),
        }
        help_texts = {
            "message_template": (
                "Pladsholdere, der udskiftes automatisk: $FORNAVN, $SALDO_LINE "
                "(sætningen om gæld/tilgodehavende), $REG_NR, $KONTO_NR."
            ),
            "followup_pdf": (
                "Valgfrit følgebrev, sættes ind som de(n) første side(r) i selve regnings-PDF'en til "
                "hver alumne — ikke en separat vedhæftning. Lad stå tom for ingen følgebrev. Opdateres "
                'typisk ca. hvert halve år. "How to aflæse din alumneregning" står altid med bagerst i '
                "PDF'en, uanset om der er et følgebrev."
            ),
        }

    def clean_followup_pdf(self) -> object:
        upload = self.cleaned_data.get("followup_pdf")
        if isinstance(upload, UploadedFile):
            name = (upload.name or "").lower()
            if upload.content_type != "application/pdf" and not name.endswith(".pdf"):
                raise forms.ValidationError("Følgebrevet skal være en PDF-fil.")
        return upload


class ResidentPasswordResetForm(PasswordResetForm):
    """Glemt-kodeord that also works for a resident who has never set one (F-014).

    Django's own `get_users` skips accounts without a usable password. New residents are created
    with exactly that (`set_unusable_password()` in the alumneliste's `add_new`) and rely on the
    welcome mail's link — which is a reset token, so it dies after PASSWORD_RESET_TIMEOUT (2h). Miss
    that window and the fallback the welcome mail itself points at silently sent nothing:
    PasswordResetView redirects to the "done" page either way, so the resident saw "vi har sendt et
    link", no mail arrived, and only a shell (`manage.py changepassword`) could let them in.

    Dropping the filter is safe here: the token still goes to the address on the account, and an
    account that cannot be logged into has nothing to protect. Django's reason for the filter is
    accounts authenticated by other means (LDAP/SSO), which GAHK has none of.
    """

    def get_users(self, email: str) -> Iterator[Resident]:
        field = Resident.get_email_field_name()
        candidates = Resident._default_manager.filter(**{f"{field}__iexact": email, "is_active": True})
        return (
            u
            for u in candidates
            # Case-insensitive identifier compare, per Django (UTR 36 §2.11.2(B)(2)) — the DB's
            # iexact is not Unicode-aware enough on its own.
            if unicodedata.normalize("NFKC", email).casefold()
            == unicodedata.normalize("NFKC", getattr(u, field)).casefold()
        )
