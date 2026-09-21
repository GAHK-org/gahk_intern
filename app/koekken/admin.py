from django.contrib import admin

from .models import KoekkenPost, Periode, Praeference, Vagt, VagtRegel, VagtTildeling


@admin.register(VagtRegel)
class VagtRegelAdmin(admin.ModelAdmin):
    list_display = ["kind", "weekend", "duration_minutes", "headcount"]
    list_filter = ["kind", "weekend"]


@admin.register(Periode)
class PeriodeAdmin(admin.ModelAdmin):
    list_display = ["kind", "year", "start_date", "end_date"]
    list_filter = ["kind"]


@admin.register(Vagt)
class VagtAdmin(admin.ModelAdmin):
    list_display = ["date", "kind", "headcount", "duration_minutes", "periode"]
    list_filter = ["kind", "periode"]
    date_hierarchy = "date"


@admin.register(VagtTildeling)
class VagtTildelingAdmin(admin.ModelAdmin):
    list_display = ["vagt", "resident", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["resident__first_name", "resident__last_name", "resident__email"]


@admin.register(KoekkenPost)
class KoekkenPostAdmin(admin.ModelAdmin):
    list_display = ["resident", "delta_minutes", "kind", "periode", "month", "created_at"]
    list_filter = ["kind", "periode"]
    search_fields = ["resident__first_name", "resident__last_name", "resident__email"]
    readonly_fields = ["created_at"]


@admin.register(Praeference)
class PraeferenceAdmin(admin.ModelAdmin):
    list_display = ["resident", "periode", "weekday_unavailable"]
    list_filter = ["periode", "weekday_unavailable"]
    search_fields = ["resident__first_name", "resident__last_name", "resident__email"]
