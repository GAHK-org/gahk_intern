"""GAHKFlix, to read. Videos, series and requests are changed on GAHKFlix itself, which records each
change in the video's history, asks for votes where the rules want them and cleans up the files; an
edit or delete here would do none of that. Tags are the exception: renaming or removing one is not
something GAHKFlix offers (the same as the photo album's)."""

from django.contrib import admin

from core.admin import ReadOnlyAdmin, ReadOnlyTabularInline

from .models import CastMember, ChangeRequest, ChangeVote, Series, Tag, Video, VideoEvent, VideoFile


class CastInline(ReadOnlyTabularInline):
    model = CastMember


class FileInline(ReadOnlyTabularInline):
    model = VideoFile
    fields = ("original_name", "state", "progress", "error", "size", "duration", "created_at")
    fk_name = "video"


class EventInline(ReadOnlyTabularInline):
    model = VideoEvent
    fields = ("created_at", "kind", "actor", "request", "changes")


@admin.register(Video)
class VideoAdmin(ReadOnlyAdmin):
    list_display = ("title", "uploaded_by", "series", "created_at", "published_at", "deleted_at")
    list_filter = ("published_at", "deleted_at")
    search_fields = ("title", "description")
    inlines = (CastInline, FileInline, EventInline)


@admin.register(Series)
class SeriesAdmin(ReadOnlyAdmin):
    list_display = ("title", "created_by", "created_at")
    search_fields = ("title",)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    search_fields = ("name",)


class VoteInline(ReadOnlyTabularInline):
    model = ChangeVote


@admin.register(ChangeRequest)
class ChangeRequestAdmin(ReadOnlyAdmin):
    list_display = ("kind", "video", "series", "requested_by", "state", "created_at")
    list_filter = ("kind", "state")
    inlines = (VoteInline,)
