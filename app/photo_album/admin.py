from django.contrib import admin

from .models import Album, Media, MediaView, Tag


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    list_display = ("name", "folder", "parent", "depth", "created_at")
    list_filter = ("folder", "depth")
    search_fields = ("name",)
    readonly_fields = ("parent", "path", "depth")
    autocomplete_fields = ("tags",)


@admin.register(Media)
class MediaAdmin(admin.ModelAdmin):
    list_display = ("filename", "album", "status", "derivative_state", "added_at")
    list_filter = ("status", "derivative_state", "is_raw")
    search_fields = ("filename", "title", "sha256")
    readonly_fields = ("uuid", "sha256", "sort_at")
    raw_id_fields = ("album", "requested_by", "approved_by", "deleted_by")
    autocomplete_fields = ("tags",)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(MediaView)
class MediaViewAdmin(admin.ModelAdmin):
    list_display = ("media", "resident", "count", "last_viewed_at")
    raw_id_fields = ("media", "resident")
