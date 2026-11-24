from django.contrib import admin

from .models import Account, Person, Residency, Resident


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("email", "first_name", "last_name", "is_staff", "is_active")
    list_filter = ("is_active",)
    search_fields = ("email", "first_name", "last_name")
    ordering = ("first_name", "last_name")
    # NOTE: password is shown raw here for now; a proper ResidentAdmin with set/change-password
    # forms comes with the auth feature build (F-014).


@admin.register(Residency)
class ResidencyAdmin(admin.ModelAdmin):
    list_display = ("resident", "year", "month", "room", "workgroup", "cleaning")
    list_filter = ("year", "month")
    search_fields = ("resident__person__first_name", "resident__person__last_name")


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("first_name", "last_name", "birthday")
    search_fields = ("first_name", "last_name")


@admin.register(Resident)
class ResidentAdmin(admin.ModelAdmin):
    list_display = ("person", "move_in_date", "move_out_date", "study")
    search_fields = ("person__first_name", "person__last_name", "study")
