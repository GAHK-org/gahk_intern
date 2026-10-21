from django.contrib import admin

from .models import CastMember, ChangeRequest, ChangeVote, Series, Tag, Video, VideoFile


class CastInline(admin.TabularInline):
    model = CastMember
    extra = 0
    raw_id_fields = ("resident",)


class FileInline(admin.TabularInline):
    model = VideoFile
    extra = 0
    fields = ("original_name", "state", "progress", "error", "size", "duration", "created_at")
    readonly_fields = fields
    can_delete = False
    fk_name = "video"


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ("title", "uploaded_by", "series", "created_at", "published_at", "deleted_at")
    list_filter = ("published_at", "deleted_at")
    search_fields = ("title", "description")
    raw_id_fields = ("uploaded_by", "deleted_by", "series", "file", "thumbnail_frame", "banner_frame")
    readonly_fields = ("uuid",)
    autocomplete_fields = ("tags",)
    inlines = (CastInline, FileInline)


@admin.register(Series)
class SeriesAdmin(admin.ModelAdmin):
    list_display = ("title", "created_by", "created_at")
    search_fields = ("title",)
    raw_id_fields = ("created_by",)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    search_fields = ("name",)


class VoteInline(admin.TabularInline):
    model = ChangeVote
    extra = 0
    raw_id_fields = ("voter",)


@admin.register(ChangeRequest)
class ChangeRequestAdmin(admin.ModelAdmin):
    list_display = ("kind", "video", "series", "requested_by", "state", "created_at")
    list_filter = ("kind", "state")
    raw_id_fields = ("video", "series", "requested_by")
    inlines = (VoteInline,)
