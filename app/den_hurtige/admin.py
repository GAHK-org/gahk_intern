from django.contrib import admin

from .models import ChannelMute, QuickComment, QuickPost


class QuickCommentInline(admin.TabularInline):
    model = QuickComment
    extra = 0
    readonly_fields = ("author", "created_at")


@admin.register(QuickPost)
class QuickPostAdmin(admin.ModelAdmin):
    """The one place a Den Hurtige message can still be destroyed.

    Residents cannot delete an archived message and moderators cannot either (den_hurtige.views),
    which is the feature working as specified. This admin deliberately keeps the delete action: an
    archive that nothing on earth can remove a row from is not a retention policy, it is a liability
    the day somebody posts something unlawful. `is_archived` is in the list so whoever is about to
    use it can see which side of the line the message is on.
    """

    list_display = ("author", "channel", "created_at", "expires_at", "is_archived")
    # `channel` is a free CharField (the registry lives in code, not this table), so the filter
    # lists the slugs actually in use rather than the ones currently defined — which is the more
    # useful question in the admin anyway: it shows leftovers from a retired channel.
    list_filter = ("channel", "created_at")
    search_fields = ("content", "author__first_name", "author__last_name", "author__email")
    readonly_fields = ("created_at",)
    inlines = [QuickCommentInline]


@admin.register(QuickComment)
class QuickCommentAdmin(admin.ModelAdmin):
    list_display = ("author", "post", "created_at", "notify_everyone")
    readonly_fields = ("created_at",)


@admin.register(ChannelMute)
class ChannelMuteAdmin(admin.ModelAdmin):
    """Mostly a support tool: "why am I not getting notifications from i-byen?" is answered here."""

    list_display = ("resident", "channel", "created_at")
    list_filter = ("channel",)
    search_fields = ("resident__first_name", "resident__last_name", "resident__email")
    readonly_fields = ("created_at",)
