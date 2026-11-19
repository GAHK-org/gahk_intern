"""Opslagstavlen, to read. Authors edit and delete their own posts and comments, and Inspektionen
moderate, on the board itself."""

from django.contrib import admin

from core.read_only_admin import ReadOnlyAdmin, ReadOnlyTabularInline

from .models import Notice, NoticeComment, NoticeImage, NoticeReaction


class NoticeCommentInline(ReadOnlyTabularInline):
    model = NoticeComment


@admin.register(Notice)
class NoticeAdmin(ReadOnlyAdmin):
    list_display = ("__str__", "category", "author", "created_at", "pinned_at", "pinned_by")
    list_filter = ("category", "created_at")
    search_fields = ("body", "author__first_name", "author__last_name", "author__email")
    inlines = [NoticeCommentInline]


@admin.register(NoticeComment)
class NoticeCommentAdmin(ReadOnlyAdmin):
    list_display = ("author", "notice", "created_at")


@admin.register(NoticeImage)
class NoticeImageAdmin(ReadOnlyAdmin):
    """`notice` is editable on purpose: it is the claim link, and re-pointing a stray upload by hand
    is the escape hatch when the Markdown and the FK have somehow disagreed."""

    list_display = ("__str__", "notice", "uploaded_by", "uploaded_at")
    list_filter = ("uploaded_at",)
    raw_id_fields = ("notice",)
    editable_fields = ("notice",)


@admin.register(NoticeReaction)
class NoticeReactionAdmin(ReadOnlyAdmin):
    list_display = ("emoji", "author", "notice", "created_at")
