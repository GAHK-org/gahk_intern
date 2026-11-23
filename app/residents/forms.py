import contextlib
import unicodedata
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm
from django.core.files.uploadedfile import UploadedFile

from access.policy import OVERRIDE_CHOICES
from core import mail as outbox
from core.uploads import validate_image_upload

from .models import Resident


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
            "access_level_override",
            "access_level_override_until",
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
            "access_level_override": "Adgang (undtagelse)",
            "access_level_override_until": "Undtagelsen gælder til og med",
        }
        help_texts = {
            "access_level_override": (
                "Kun for den, alumnelisten ikke passer på. Tom: adgangen følger alumnelisten."
            ),
            "access_level_override_until": "Tom: indtil undtagelsen fjernes.",
        }
        widgets = {
            "birthday": LocalDateInput(),
            "move_in_date": LocalDateInput(),
            "move_out_date": LocalDateInput(),
            "access_level_override_until": LocalDateInput(),
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
        override = self.fields["access_level_override"]
        if isinstance(override, forms.ChoiceField):
            override.choices = [("", "Følger alumnelisten"), *OVERRIDE_CHOICES]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if not data.get("access_level_override"):
            data["access_level_override_until"] = None  # an end date with nothing to end
        return data


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


def password_link_mail() -> contextlib.AbstractContextManager[None]:
    """Mail queued inside this carries a set-your-password link, and someone is waiting for it: it is
    sent at once (core.mail's interactive kind), and given up on after half the link's lifetime —
    arriving later, it would leave the resident too little time to use it."""
    return outbox.kind(
        outbox.Kind.INTERACTIVE, lifetime=timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT) / 2
    )


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

    def send_mail(
        self,
        subject_template_name: str,
        email_template_name: str,
        context: dict[str, Any],
        from_email: str | None,
        to_email: str,
        html_email_template_name: str | None = None,
    ) -> None:
        with password_link_mail():
            super().send_mail(
                subject_template_name,
                email_template_name,
                context,
                from_email,
                to_email,
                html_email_template_name,
            )
