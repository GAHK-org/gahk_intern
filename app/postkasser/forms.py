from typing import cast

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Q

from residents.models import Resident

from . import access, crypto, rules, services
from .models import BoardColumn, Mailbox, MailFolder, MailRule, MailTag, MailTimeRule

AUTOMATIC_SENT = "Automatisk (serverens Sendt-mappe)"


def _addresses(value: str) -> list[str]:
    parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    for part in parts:
        try:
            validate_email(part)
        except ValidationError as exc:
            raise ValidationError(f"«{part}» er ikke en gyldig mailadresse.") from exc
    return parts


class ComposeForm(forms.Form):
    to = forms.CharField(label="Til", help_text="Flere adresser adskilles med komma.")
    cc = forms.CharField(label="Cc", required=False)
    subject = forms.CharField(label="Emne", max_length=500)
    body = forms.CharField(label="Besked", widget=forms.Textarea(attrs={"rows": 14}))

    def clean_subject(self) -> str:
        return " ".join(self.cleaned_data["subject"].replace("\r", " ").replace("\n", " ").split())

    def clean_to(self) -> list[str]:
        addresses = _addresses(self.cleaned_data["to"])
        if not addresses:
            raise ValidationError("Angiv mindst én modtager.")
        return addresses

    def clean_cc(self) -> list[str]:
        return _addresses(self.cleaned_data["cc"])


class MailboxForm(forms.ModelForm):
    """Admin form: the one.com password is WRITE-ONLY. It is never rendered back (not even as
    ciphertext); leaving it blank on edit keeps the stored one."""

    password = forms.CharField(
        label="Adgangskode (one.com)",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Lad stå tomt for at beholde den gemte adgangskode.",
    )
    # Not free text: a name typed here ("Sendt") is rarely what the server calls the folder
    # ("INBOX.Sent"). The choices are the mailbox's own synced folders; "" = the folder the server marks Sent.
    sent_folder = forms.ChoiceField(
        label="Mappe til sendt post",
        required=False,
        help_text="Mapperne kendes først efter postkassens første hentning; indtil da kan kun «Automatisk» vælges.",
    )

    class Meta:
        model = Mailbox
        fields = [
            "name", "slug", "address", "username", "password", "imap_host", "imap_port", "smtp_host",
            "smtp_port", "sent_folder", "signature", "workgroups", "roles", "is_active",
        ]  # fmt: skip

    # Where the stored password is sent to, and as whom. Pointing any of these somewhere else while
    # keeping the old password would let an admin read it off a server of their own.
    LOGIN_TARGET_FIELDS = ("address", "username", "imap_host", "imap_port", "smtp_host", "smtp_port")

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        choices = [("", AUTOMATIC_SENT)]
        if self.instance.pk:
            folders = (
                self.instance.folders.filter(selectable=True)
                .exclude(special_use__in=services.SKIPPED_USES)
                .order_by("path")
            )
            for folder in folders:
                label = folder.path if folder.path == folder.name else f"{folder.path} — {folder.name}"
                choices.append((folder.name, label))
            stored = self.instance.sent_folder
            if stored and stored not in {value for value, _label in choices}:
                # Kept selectable so that saving something else does not silently change it.
                unsuitable = services.sent_folder_exists_unsuitably(self.instance)
                note = "ikke egnet som Sendt-mappe" if unsuitable else "findes ikke på serveren"
                choices.append((stored, f"{stored} ({note})"))
        cast(forms.ChoiceField, self.fields["sent_folder"]).choices = choices

    def clean_sent_folder(self) -> str:
        folder = self.cleaned_data["sent_folder"]
        if not folder.isascii():
            raise ValidationError("Mappenavnet skal være uden æ/ø/å — brug navnet som serveren viser det.")
        return folder

    def clean(self) -> dict:
        cleaned = super().clean() or {}
        if not cleaned.get("password"):
            if not self.instance.password_encrypted:
                self.add_error("password", "En ny postkasse skal have en adgangskode.")
            elif set(self.LOGIN_TARGET_FIELDS) & set(self.changed_data):
                self.add_error(
                    "password",
                    "Når server, port, brugernavn eller adresse ændres, skal adgangskoden indtastes igen.",
                )
        return cleaned

    def save(self, commit: bool = True) -> Mailbox:
        mailbox = super().save(commit=False)
        if password := self.cleaned_data.get("password"):
            mailbox.password_encrypted = crypto.encrypt(password)
        if commit:
            mailbox.save()
            self.save_m2m()
        return mailbox


class RuleForm(forms.ModelForm):
    """A rule of ONE mailbox: every choice is limited to what that mailbox has, so a pk of another group's
    tag, column, folder or member is simply "not a valid choice". At least one condition and one action."""

    class Meta:
        model = MailRule
        fields = [
            "name",
            "from_contains",
            "subject_contains",
            "to_contains",
            "folder",
            "add_tags",
            "set_column",
            "set_assignee",
            "stop_processing",
            "is_active",
        ]
        widgets = {"add_tags": forms.CheckboxSelectMultiple}

    def __init__(self, *args: object, mailbox: Mailbox, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.mailbox = mailbox
        folder, tags, column, assignee = (
            cast(forms.ModelChoiceField, self.fields[name])
            for name in ("folder", "add_tags", "set_column", "set_assignee")
        )
        folder.queryset = MailFolder.objects.filter(mailbox=mailbox, selectable=True)
        tags.queryset = MailTag.objects.filter(mailbox=mailbox)
        column.queryset = BoardColumn.objects.filter(mailbox=mailbox)
        # The rule's current assignee stays in the list even if they have left the group, so the stored
        # value is visible (and refused, see clean_set_assignee) instead of silently dropped.
        self._member_ids = set(access.members(mailbox).values_list("pk", flat=True))
        self._current_assignee = self.instance.set_assignee_id if self.instance.pk else None
        assignee.queryset = Resident.objects.filter(
            Q(pk__in=self._member_ids) | Q(pk=self._current_assignee)
        ).order_by("first_name", "last_name", "pk")
        folder.empty_label = "Alle mapper"
        column.empty_label = assignee.empty_label = "Ingen ændring"
        folder.label_from_instance = lambda f: f"{'  ' * f.depth}{f.display_name}"  # type: ignore[method-assign,assignment]
        assignee.label_from_instance = lambda r: (  # type: ignore[method-assign,assignment]
            r.full_name if r.pk in self._member_ids else f"{r.full_name} (ikke længere i gruppen)"
        )

    def clean_set_assignee(self) -> Resident | None:
        assignee = self.cleaned_data.get("set_assignee")
        if assignee is not None and assignee.pk not in self._member_ids:
            raise ValidationError(
                f"{assignee.full_name} kan ikke længere åbne postkassen. Vælg en anden, eller fjern ansvarlig."
            )
        return assignee

    def clean(self) -> dict:
        data = super().clean() or {}
        if self.errors:
            return data
        for name in ("from_contains", "subject_contains", "to_contains"):
            data[name] = " ".join(data.get(name, "").split())
        if not (
            data["from_contains"] or data["subject_contains"] or data["to_contains"] or data.get("folder")
        ):
            raise ValidationError("Reglen skal have mindst én betingelse — ellers ville den ramme al post.")
        if not (data.get("add_tags") or data.get("set_column") or data.get("set_assignee")):
            raise ValidationError(
                "Reglen skal gøre noget: tilføje tags, flytte sagen eller gøre nogen ansvarlig."
            )
        return data


class TimeRuleForm(forms.ModelForm):
    class Meta:
        model = MailTimeRule
        fields = ["name", "from_column", "after_days", "to_column", "is_active"]

    def __init__(self, *args: object, mailbox: Mailbox, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.mailbox = mailbox
        for name in ("from_column", "to_column"):
            cast(forms.ModelChoiceField, self.fields[name]).queryset = BoardColumn.objects.filter(
                mailbox=mailbox
            )

    def clean(self) -> dict:
        data = super().clean() or {}
        if data.get("from_column") and data.get("from_column") == data.get("to_column"):
            raise ValidationError("Fra- og til-kolonnen skal være forskellige.")
        if data.get("is_active") and data.get("from_column") and data.get("to_column"):
            other = rules.time_rule_conflict(
                self.mailbox, data["from_column"], data["to_column"], except_pk=self.instance.pk
            )
            if other is not None:
                raise ValidationError(
                    f"Tidsreglen «{other.name}» flytter den modsatte vej; de to ville flytte sagerne frem og tilbage."
                )
        return data
