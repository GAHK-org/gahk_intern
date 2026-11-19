"""The photo album, to read. Albums and pictures are changed in the album itself, which keeps the
album tree, the bins and the stored files in step; an edit or delete here would not. Tags are the
exception: the album never removes or renames one, so administrators do it here."""

from django.contrib import admin

from core.read_only_admin import ReadOnlyAdmin

from .models import Album, Media, MediaView, Tag


@admin.register(Album)
class AlbumAdmin(ReadOnlyAdmin):
    list_display = ("name", "folder", "parent", "depth", "created_at")
    list_filter = ("folder", "depth")
    search_fields = ("name",)


@admin.register(Media)
class MediaAdmin(ReadOnlyAdmin):
    list_display = ("filename", "album", "status", "derivative_state", "added_at")
    list_filter = ("status", "derivative_state", "is_raw")
    search_fields = ("filename", "title", "sha256")


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(MediaView)
class MediaViewAdmin(ReadOnlyAdmin):
    list_display = ("media", "resident", "count", "last_viewed_at")
