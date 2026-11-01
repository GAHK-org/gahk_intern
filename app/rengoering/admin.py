"""Areas and task lists are Inspektionen's to edit, here in the Django admin — a sortable nested list
needed no UI of its own. Gated like cms.admin.ContentEditorAdmin: role-holders have `is_staff` but no
Django permission bits, so every check is the role check."""

from django.contrib import admin
from django.http import HttpRequest

from residents.models import Role
from residents.permissions import has_active_role

from .models import CleaningArea, CleaningTask

MANAGE_ROLES = (Role.INSPEKTION, Role.ADMINISTRATOR)


class _InspektionOnly:
    def _may(self, request: HttpRequest) -> bool:
        return has_active_role(request.user, *MANAGE_ROLES)

    def has_module_permission(self, request: HttpRequest) -> bool:
        return self._may(request)

    def has_view_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return self._may(request)

    def has_add_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return self._may(request)

    def has_change_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return self._may(request)

    def has_delete_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return self._may(request)


class CleaningTaskInline(_InspektionOnly, admin.TabularInline):
    model = CleaningTask
    fields = ["order", "zone", "group", "text", "star_only", "is_active"]
    extra = 0


@admin.register(CleaningArea)
class CleaningAreaAdmin(_InspektionOnly, admin.ModelAdmin):
    list_display = ["name", "description", "order", "is_active"]
    list_editable = ["order", "is_active"]
    prepopulated_fields = {"slug": ["name"]}
    inlines = [CleaningTaskInline]
