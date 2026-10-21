from django.contrib import admin

from .models import Residency, Resident, RoleAssignment


@admin.register(Resident)
class ResidentAdmin(admin.ModelAdmin):
    list_display = ("email", "first_name", "last_name", "is_staff", "is_active", "is_kitchen")
    list_filter = ("is_kitchen", "is_active")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("first_name", "last_name")
    # NOTE: password is shown raw here for now; a proper ResidentAdmin with set/change-password
    # forms comes with the auth feature build (F-014).


@admin.register(Residency)
class ResidencyAdmin(admin.ModelAdmin):
    list_display = ("resident", "year", "month", "room", "workgroup", "cleaning")
    list_filter = ("year", "month")
    search_fields = ("resident__email", "resident__first_name", "resident__last_name")


@admin.register(RoleAssignment)
class RoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ("resident", "role", "year", "month")
    list_filter = ("role", "year", "month")
    search_fields = ("resident__email", "resident__first_name", "resident__last_name")
