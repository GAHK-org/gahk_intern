"""Read-only: bookings are made, approved and cancelled on the site, where the overlap rule and the
audit trail apply. The admin is for looking."""

from django.contrib import admin
from django.db.models import Model
from django.http import HttpRequest

from .models import Booking, Facility


class _ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False


@admin.register(Facility)
class FacilityAdmin(_ReadOnly):
    list_display = ["name", "capacity", "requires_approval", "is_active"]


@admin.register(Booking)
class BookingAdmin(_ReadOnly):
    list_display = ["facility", "starts_at", "ends_at", "booked_by", "status", "event"]
    list_filter = ["facility", "status"]
    date_hierarchy = "starts_at"
