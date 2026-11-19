from django.contrib import admin

from core.admin import ReadOnlyAdmin

from .models import RepairTask


@admin.register(RepairTask)
class RepairTaskAdmin(ReadOnlyAdmin):
    """Repairs are reported, assigned, archived and deleted on /reparationer."""

    list_display = ["title", "location", "status", "responsible", "reported_by", "created_at"]
    list_filter = ["status", "responsible"]
    search_fields = ["title", "location", "description"]
