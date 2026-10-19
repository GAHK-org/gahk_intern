from django.contrib import admin

from .models import Notice, NoticeComment, NoticeImage, NoticeReaction


class NoticeCommentInline(admin.TabularInline):
    model = NoticeComment
    extra = 0
    readonly_fields = ("author", "created_at")


@admin.register(Notice)
class NoticeAdmin(admin.ModelAdmin):
    list_display = ("__str__", "category", "author", "created_at", "pinned_at", "pinned_by")
    list_filter = ("category", "created_at")
    search_fields = ("body", "author__first_name", "author__last_name", "author__email")
    readonly_fields = ("created_at", "edited_at")
    inlines = [NoticeCommentInline]


@admin.register(NoticeComment)
class NoticeCommentAdmin(admin.ModelAdmin):
    list_display = ("author", "notice", "created_at")
    readonly_fields = ("created_at",)


@admin.register(NoticeImage)
class NoticeImageAdmin(admin.ModelAdmin):
    """`notice` is editable on purpose: it is the claim link, and re-pointing a stray upload by hand
    is the escape hatch when the Markdown and the FK have somehow disagreed."""

    list_display = ("__str__", "notice", "uploaded_by", "uploaded_at")
    list_filter = ("uploaded_at",)
    readonly_fields = ("uploaded_at", "uploaded_by")


@admin.register(NoticeReaction)
class NoticeReactionAdmin(admin.ModelAdmin):
    list_display = ("emoji", "author", "notice", "created_at")
    readonly_fields = ("created_at",)
