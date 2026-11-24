from django.contrib import admin

from .models import Grant


@admin.register(Grant)
class GrantAdmin(admin.ModelAdmin):
    list_display = ("account", "role", "starts_on", "ends_on", "reason")
    list_filter = ("role",)
    search_fields = ("account__email", "account__first_name", "account__last_name")
    raw_id_fields = ("account", "granted_by")
