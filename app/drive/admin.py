from django.contrib import admin

from .models import Event, File, FileVersion, Folder


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
