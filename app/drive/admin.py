from django.contrib import admin

from .models import (
    ApprovalRequest,
    Event,
    File,
    FileVersion,
    Folder,
    SharedFolder,
    SharedMember,
    StandingFolder,
)


@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "space", "workgroup", "period_start", "path", "deleted_at")
    list_filter = ("kind", "space")
    search_fields = ("name",)
    raw_id_fields = ("parent", "created_by", "deleted_by")


class FileVersionInline(admin.TabularInline):
    model = FileVersion
    extra = 0
    readonly_fields = ("number", "key", "size", "content_type", "created_by", "created_at", "restored_from")


@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    list_display = ("name", "folder", "size", "changed_at", "deleted_at")
    search_fields = ("name",)
    raw_id_fields = ("folder", "created_by", "changed_by", "deleted_by")
    inlines = [FileVersionInline]


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("at", "action", "name", "actor")
    list_filter = ("action",)
    search_fields = ("name",)
    raw_id_fields = ("actor", "file", "folder")


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("requested_at", "kind", "state", "name", "requested_by", "decided_by")
    list_filter = ("kind", "state")
    search_fields = ("name", "where")
    raw_id_fields = ("file", "folder", "requested_by", "decided_by")


class SharedMemberInline(admin.TabularInline):
    model = SharedMember
    extra = 0
    raw_id_fields = ("resident",)


@admin.register(SharedFolder)
class SharedFolderAdmin(admin.ModelAdmin):
    list_display = ("tag", "expires_on", "on_expiry", "archived_at")
    list_filter = ("on_expiry",)
    raw_id_fields = ("created_by",)
    inlines = [SharedMemberInline]


@admin.register(StandingFolder)
class StandingFolderAdmin(admin.ModelAdmin):
    list_display = ("name", "space", "workgroup", "starts_on", "limited", "stopped_at")
    list_filter = ("space", "limited")
    filter_horizontal = ("writer_groups",)
    raw_id_fields = ("created_by",)
