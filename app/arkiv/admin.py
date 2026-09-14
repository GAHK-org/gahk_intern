"""Admin for the root folders, and the escape hatch for everything else.

Root folders are the kollegium's filing system - one per embedsgruppe plus the shared areas - and
arkiv.access.can_manage_roots says they are Inspektionen's and Netvaerksgruppen's to arrange. There
is no custom UI for that in this slice: the admin already does it, and a screen that exists to be
used a few times a year is not worth building twice.

It is also the deliberate escape hatch access.py names. No role gets blanket read access through the
feature itself - a group folder's promise is that non-members cannot read it, and "except
Inspektionen" would make that false in the only case anyone cares about. A genuinely misfiled
document is fixed here instead, in the admin, which has always seen every table.
"""

from django.contrib import admin
from django.http import HttpRequest

from residents.models import Role
from residents.permissions import has_active_role

from .models import ArchiveFile, ArchiveFolder

MANAGE_ROLES = (Role.ADMINISTRATOR, Role.INSPEKTION)


class _RoleGated(admin.ModelAdmin):
    def has_module_permission(self, request: HttpRequest) -> bool:
        return has_active_role(request.user, *MANAGE_ROLES)

    def has_view_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return has_active_role(request.user, *MANAGE_ROLES)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return has_active_role(request.user, *MANAGE_ROLES)

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return has_active_role(request.user, *MANAGE_ROLES)

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return has_active_role(request.user, *MANAGE_ROLES)


@admin.register(ArchiveFolder)
class ArchiveFolderAdmin(_RoleGated):
    list_display = ("name", "parent", "workgroup", "effective_workgroup", "deleted_at")
    list_filter = ("workgroup", "effective_workgroup")
    search_fields = ("name",)
    autocomplete_fields = ()
    readonly_fields = ("effective_workgroup", "created_at")

    def save_model(self, request: HttpRequest, obj: ArchiveFolder, form: object, change: bool) -> None:
        """Save, then re-resolve everything underneath.

        ArchiveFolder.save() fixes this row; changing an embedsgruppe here changes what every
        descendant inherits, and visible_folders reads only the denormalised column. A stale subtree
        is not cosmetic - it is folders invisible to their owners, or visible to people who are not.
        """
        from .services import reassign_subtree

        if not change:
            obj.created_by = request.user if request.user.is_authenticated else None
        super().save_model(request, obj, form, change)
        reassign_subtree(obj)


@admin.register(ArchiveFile)
class ArchiveFileAdmin(_RoleGated):
    list_display = ("name", "folder", "size", "uploaded_by", "uploaded_at", "deleted_at")
    search_fields = ("name", "sha256")
    readonly_fields = ("sha256", "size", "content_type", "uploaded_at")
