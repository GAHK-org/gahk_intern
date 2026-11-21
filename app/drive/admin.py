from django.contrib import admin

from .models import File, FileVersion, Folder


@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "space", "workgroup", "period_start", "path")
    list_filter = ("kind", "space")
    search_fields = ("name",)
    raw_id_fields = ("parent", "created_by")


class FileVersionInline(admin.TabularInline):
    model = FileVersion
    extra = 0
    readonly_fields = ("number", "key", "size", "content_type", "created_by", "created_at")


@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    list_display = ("name", "folder", "size", "changed_at")
    search_fields = ("name",)
    raw_id_fields = ("folder", "created_by", "changed_by")
    inlines = [FileVersionInline]
