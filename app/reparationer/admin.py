from django.contrib import admin

from .models import RepairTask


@admin.register(RepairTask)
class RepairTaskAdmin(admin.ModelAdmin):
    list_display = ["title", "location", "status", "responsible", "reported_by", "created_at"]
    list_filter = ["status", "responsible"]
    search_fields = ["title", "location", "description"]
