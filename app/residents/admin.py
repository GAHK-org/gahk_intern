from django.contrib import admin
from django.http import HttpRequest

from .models import Residency, Resident, RoleAssignment


@admin.register(Resident)
class ResidentAdmin(admin.ModelAdmin):
    """An explicit field list, because the default form is a privilege escalation: an approved
    administrator holds every model permission (Resident.has_perm), and the auto-generated form
    offered `is_superuser`, `groups` and `user_permissions` — one tick away from making themselves
    (or anyone) a permanent superuser that outlives the monthly role. Those three are not editable
    here at all; superuser status is granted with `createsuperuser` on the server. The password hash
    is never shown or editable either: residents set their own via "glemt adgangskode"."""

    list_display = ("email", "first_name", "last_name", "is_staff", "is_active")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("first_name", "last_name")
    fields = (
        "email",
        "first_name",
        "last_name",
        "phone",
        "birthday",
        "move_in_date",
        "move_out_date",
        "study",
        "sponsor",
        "fylgje_raw",
        "profile_picture",
        "bio",
        "facebook_link",
        "instagram_handle",
        "is_active",
        "is_staff",
        "has_password",
        "last_login",
        "date_joined",
    )
    readonly_fields = ("has_password", "last_login", "date_joined")

    @admin.display(description="Adgangskode", boolean=True)
    def has_password(self, obj: Resident) -> bool:
        # `bool(password)`: a resident added here has an empty hash, which Django counts as usable.
        return bool(obj.password) and obj.has_usable_password()

    def has_change_permission(self, request: HttpRequest, obj: Resident | None = None) -> bool:
        """Only a superuser may edit or delete a superuser: otherwise an administrator could change
        the superuser's email to their own, reset the password and log in as them."""
        if obj is not None and obj.is_superuser and not request.user.is_superuser:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Resident | None = None) -> bool:
        if obj is not None and obj.is_superuser and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)


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
