from django.contrib import admin

from .models import Album, Media, MediaView


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    list_display = ("name", "folder", "parent", "depth", "created_at")
    list_filter = ("folder", "depth")
    search_fields = ("name",)
    readonly_fields = ("parent", "path", "depth")


@admin.register(Media)
class MediaAdmin(admin.ModelAdmin):
    list_display = ("filename", "album", "status", "derivative_state", "added_at")
    list_filter = ("status", "derivative_state")
    search_fields = ("filename", "title", "sha256")
    readonly_fields = ("uuid", "sha256", "sort_at")
    raw_id_fields = ("album", "requested_by", "approved_by", "deleted_by")


@admin.register(MediaView)
class MediaViewAdmin(admin.ModelAdmin):
    list_display = ("media", "resident", "count", "last_viewed_at")
    raw_id_fields = ("media", "resident")
