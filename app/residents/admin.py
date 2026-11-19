"""Residents, to read — with the two switches nothing else on the site has.

Everything else about a resident has a page of its own: Indstillingen edit their details and the
monthly list (room, embedsgruppe, rengøring), the resident edits their profile, roles are given on
the site-admin's roles page, and regnskab sets the e-conomic account number. Those pages also do what
an edit here would skip: the role page records who gave `administrator`, which the two-person rule
on Django-admin access depends on (residents.admin_access).

That rule is also why `is_superuser`, `is_staff`, groups and permissions are read-only here: an
administrator who could tick them on their own row would not need the second administrator at all.
"""

from django.contrib import admin

from core.read_only_admin import ReadOnlyAdmin

from .models import Residency, Resident, RoleAssignment


@admin.register(Resident)
class ResidentAdmin(ReadOnlyAdmin):
    """`is_active` closes a login (nothing else does), and `is_kitchen` marks the shared kitchen
    account (core.middleware.KitchenAccountGate). No adding or deleting: residents come from the
    monthly list, and deleting one would take every post, comment and picture they made with them.
    The password hash is left out of the page altogether."""

    list_display = ("email", "first_name", "last_name", "is_staff", "is_active", "is_kitchen")
    list_filter = ("is_kitchen", "is_active")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("first_name", "last_name")
    exclude = ("password",)
    editable_fields = ("is_active", "is_kitchen")


@admin.register(Residency)
class ResidencyAdmin(ReadOnlyAdmin):
    list_display = ("resident", "year", "month", "room", "workgroup", "cleaning")
    list_filter = ("year", "month")
    search_fields = ("resident__email", "resident__first_name", "resident__last_name")


@admin.register(RoleAssignment)
class RoleAssignmentAdmin(ReadOnlyAdmin):
    list_display = ("resident", "role", "year", "month")
    list_filter = ("role", "year", "month")
    search_fields = ("resident__email", "resident__first_name", "resident__last_name")
