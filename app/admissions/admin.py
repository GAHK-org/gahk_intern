from django.contrib import admin

from core.read_only_admin import ReadOnlyAdmin

from .models import Application


@admin.register(Application)
class ApplicationAdmin(ReadOnlyAdmin):
    """Applications are handled on /optagelse/listansoegninger. Deleting is kept: it is the only way
    to erase an applicant who asks to be forgotten (the list's "kassér" only hides the application)."""

    list_display = ("full_name", "type", "email", "submitted_at", "received_by")
    list_filter = ("type", "gender")
    search_fields = ("full_name", "email", "university")
    date_hierarchy = "submitted_at"
    deletable = True
