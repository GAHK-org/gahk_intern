from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.forms import BaseForm
from django.forms.models import BaseInlineFormSet
from django.http import HttpRequest

from . import columns, services, tags
from .forms import MailboxForm
from .models import BoardColumn, ColumnRole, Mailbox, MailEvent, MailRule, MailTag, MailThread, MailTimeRule


def _deleted(form: BaseForm) -> bool:
    return bool(getattr(form, "cleaned_data", {}).get("DELETE"))


class BoardColumnFormSet(BaseInlineFormSet):
    """The same invariants the "Kolonner" page keeps (columns.validate_layout), checked on the whole
    edited set — a per-row check cannot see "the last Ny column was just deleted"."""

    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return
        kept = [
            (f.cleaned_data["name"], f.cleaned_data.get("role", ""))
            for f in self.forms
            if f.cleaned_data and not f.cleaned_data.get("DELETE")
        ]
        try:
            columns.validate_layout(kept)
        except columns.ColumnError as exc:
            raise ValidationError(str(exc)) from exc
        if self._deleted_columns_with_sager() and self._ny_holder() is None:
            raise ValidationError(
                "En kolonne med sager kan kun slettes, hvis funktionen «Indgående» ligger på en af de "
                "kolonner der allerede findes — sagerne flyttes derhen. Gem først funktionen, og slet bagefter."
            )

    def _deleted_columns_with_sager(self) -> list[BoardColumn]:
        deleted = [f.instance for f in self.initial_forms if _deleted(f) and f.instance.pk]
        return [c for c in deleted if MailThread.objects.filter(column=c).exists()]

    def _ny_holder(self) -> BoardColumn | None:
        """The existing column that will hold `ny` once this form is saved (None if it is a new row)."""
        for form in self.initial_forms:
            if not _deleted(form) and form.cleaned_data.get("role") == ColumnRole.NY:
                return form.instance
        return None

    def save(self, commit: bool = True) -> list[BoardColumn]:
        """Roles are held by one column at a time (a partial unique index), so saving the rows one by
        one fails on a swap — the first row saved takes a role the second still holds. Every row that
        is about to change (or go) lets go of its role first, inside one transaction; rows left alone
        keep theirs, and the layout check above has already made sure each role ends up held once.
        A deleted column's sager go to the column that gets `ny`, as on the Kolonner page."""
        if not commit:
            return super().save(commit=False)
        with transaction.atomic():
            touched = [
                f.instance.pk
                for f in self.initial_forms
                if f.instance.pk and (f.has_changed() or _deleted(f))
            ]
            self.instance.columns.filter(pk__in=touched).update(role="")
            target = self._ny_holder()
            if target is not None:
                for column in self._deleted_columns_with_sager():
                    MailThread.objects.filter(column=column).update(column=target)
            for form in self.initial_forms:
                if _deleted(form) and form.instance.pk:
                    columns.deactivate_time_rules(form.instance)
            return super().save(commit=True)


class BoardColumnInline(admin.TabularInline):
    model = BoardColumn
    formset = BoardColumnFormSet
    extra = 0
    fields = ["name", "position", "role"]


class MailTagFormSet(BaseInlineFormSet):
    """Names are unique per mailbox ignoring case (tags.validate_names)."""

    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return
        try:
            tags.validate_names(
                f.cleaned_data["name"] for f in self.forms if f.cleaned_data and not _deleted(f)
            )
        except tags.TagError as exc:
            raise ValidationError(str(exc)) from exc


class MailTagInline(admin.TabularInline):
    model = MailTag
    formset = MailTagFormSet
    extra = 0
    fields = ["name", "color", "position"]


@admin.register(Mailbox)
class MailboxAdmin(admin.ModelAdmin):
    form = MailboxForm
    list_display = ["name", "address", "is_active", "last_synced_at", "last_sync_error", "sent_folder_stale"]
    readonly_fields = ["last_synced_at", "last_sync_error"]
    prepopulated_fields = {"slug": ["name"]}
    filter_horizontal = ["workgroups"]
    inlines = [BoardColumnInline, MailTagInline]

    @admin.display(description="Sendt-mappe findes ikke")
    def sent_folder_stale(self, obj: Mailbox) -> str:
        """A warning when the folder named on the mailbox is not on the server, or is no place for sent
        mail (mails go to the server's Sent folder instead, see services.sent_folder_name); empty otherwise."""
        if not services.sent_folder_is_stale(obj):
            return ""
        if services.sent_folder_exists_unsuitably(obj):
            return f"⚠ «{obj.sent_folder}» er ikke egnet som Sendt-mappe"
        return f"⚠ «{obj.sent_folder}» findes ikke på serveren"

    def get_inline_instances(self, request: HttpRequest, obj: Mailbox | None = None) -> list:
        # A new mailbox gets its default columns when it is saved (models.create_default_columns); an
        # inline on the add page would race them for the same names.
        return super().get_inline_instances(request, obj) if obj is not None else []


@admin.register(MailEvent)
class MailEventAdmin(admin.ModelAdmin):
    list_display = ["created_at", "mailbox", "action", "resident", "subject"]
    list_filter = ["mailbox", "action"]

    def has_add_permission(self, request: object) -> bool:  # the log is written by the views only
        return False

    def has_change_permission(self, request: object, obj: object = None) -> bool:
        return False


@admin.register(MailRule)
class MailRuleAdmin(admin.ModelAdmin):
    """For support; the groups edit their rules on the Automatisering page, which limits every choice to
    the rule's own mailbox — here nothing does, so prefer that page."""

    list_display = ["name", "mailbox", "position", "is_active"]
    list_filter = ["mailbox"]


@admin.register(MailTimeRule)
class MailTimeRuleAdmin(admin.ModelAdmin):
    list_display = ["name", "mailbox", "from_column", "after_days", "to_column", "is_active"]
    list_filter = ["mailbox"]
