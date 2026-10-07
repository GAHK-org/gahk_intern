from django.contrib import admin

from .models import KitchenAssignment, KitchenPointEntry, KitchenShift


class KitchenAssignmentInline(admin.TabularInline):
    model = KitchenAssignment
    extra = 0
    autocomplete_fields = ["resident"]


@admin.register(KitchenShift)
class KitchenShiftAdmin(admin.ModelAdmin):
    list_display = ["date", "kind", "starts_at", "spots", "points_per_spot", "bonus_points", "is_disabled"]
    list_filter = ["kind", "is_disabled"]
    date_hierarchy = "date"
    inlines = [KitchenAssignmentInline]


@admin.register(KitchenPointEntry)
class KitchenPointEntryAdmin(admin.ModelAdmin):
    list_display = ["created_at", "resident", "kind", "balance_before", "amount", "balance_after", "message"]
    list_filter = ["kind"]
    search_fields = ["resident__first_name", "resident__last_name", "message"]

    # Append-only: editing a row would break the balance_before/after chain.
    def has_change_permission(self, request: object, obj: object = None) -> bool:
        return False

    def has_add_permission(self, request: object) -> bool:
        return False

    def has_delete_permission(self, request: object, obj: object = None) -> bool:
        return False
