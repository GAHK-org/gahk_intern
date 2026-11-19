"""The Django admin's default: look, don't touch.

Every feature has its own screens, and those are where its rows are written — with the checks,
history, notifications and file clean-up that go with them. The Django admin skips all of that, so
a row edited there is a row the feature never saw change. It stays useful for looking things up,
and for the few changes nothing else on the site can make; those admins say which, and why.

`ReadOnlyAdmin` turns off adding, changing and deleting. An admin opens a narrow door with
`deletable` (the escape hatch for content nobody else may remove) or `editable_fields` (the fields
with no other place to be edited); every other field stays read-only.
"""

from collections.abc import Sequence

from django.contrib import admin
from django.db.models import Model
from django.http import HttpRequest


class ReadOnlyAdmin(admin.ModelAdmin):
    deletable = False
    editable_fields: tuple[str, ...] = ()

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return bool(self.editable_fields) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return self.deletable and super().has_delete_permission(request, obj)

    def get_readonly_fields(self, request: HttpRequest, obj: Model | None = None) -> list[str]:
        """Everything on the model except `editable_fields` (and what `exclude` hides)."""
        hidden = {*self.editable_fields, *(self.exclude or ())}
        return _all_fields(self.model, hidden, super().get_readonly_fields(request, obj))


def _all_fields(model: type[Model], hidden: set[str], declared: Sequence[str]) -> list[str]:
    opts = model._meta
    fields = [
        f.name for f in [*opts.fields, *opts.many_to_many] if not f.auto_created and f.name not in hidden
    ]
    return [*dict.fromkeys([*fields, *declared])]


class ReadOnlyTabularInline(admin.TabularInline):
    """The rows under a parent, to read. Removing one is a job for the feature (or the row's own admin)."""

    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: Model | None = None) -> list[str]:
        # All of them, not just the shown ones: a non-editable field in `fields` must be read-only
        # or the formset cannot be built.
        return _all_fields(self.model, set(self.exclude or ()), super().get_readonly_fields(request, obj))
