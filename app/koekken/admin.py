from django.contrib import admin

from .models import (
    Fridag,
    KoekkenPost,
    Periode,
    Praeference,
    PraeferenceDag,
    Vagt,
    VagtAnmeldelse,
    VagtRegel,
    VagtTildeling,
)


@admin.register(VagtRegel)
class VagtRegelAdmin(admin.ModelAdmin):
    list_display = ["kind", "weekend", "start_time", "duration_minutes", "headcount"]
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
    list_display = ["resident", "periode", "weekday_unavailable", "declared_at"]
    list_filter = ["periode", "weekday_unavailable"]
    search_fields = ["resident__first_name", "resident__last_name", "resident__email"]


@admin.register(PraeferenceDag)
class PraeferenceDagAdmin(admin.ModelAdmin):
    list_display = ["praeference", "kind", "weekday"]
    list_filter = ["kind", "weekday"]


@admin.register(VagtAnmeldelse)
class VagtAnmeldelseAdmin(admin.ModelAdmin):
    list_display = ["vagt_tildeling", "flagged_by", "previous_status", "status", "created_at"]
    list_filter = ["status", "previous_status"]
    search_fields = ["flagged_by__first_name", "flagged_by__last_name", "flagged_by__email"]
    readonly_fields = ["created_at"]


@admin.register(Fridag)
class FridagAdmin(admin.ModelAdmin):
    list_display = ["date", "kind", "reason"]
    list_filter = ["kind"]
    date_hierarchy = "date"
