from django.urls import path

from . import views

app_name = "kitchen"

urlpatterns = [
    path("", views.market, name="market"),
    path("raekker", views.sheet_rows, name="sheet_rows"),
    path("historik", views.history, name="history"),
    path("konto", views.account, name="account"),
    path("push", views.save_subscription, name="save_subscription"),
    path("vagt/<int:pk>", views.shift_detail, name="shift"),
    path("vagt/<int:pk>/tilmeld", views.enroll, name="enroll"),
    path("vagt/<int:pk>/aflys", views.disable, name="disable"),
    path("vagt/<int:pk>/genaabn", views.enable, name="enable"),
    path("vagt/<int:pk>/bonus", views.set_bonus, name="set_bonus"),
    path("tildeling/<int:pk>/afmeld", views.unenroll, name="unenroll"),
    path("tildeling/<int:pk>/saelg", views.sell, name="sell"),
    path("tildeling/<int:pk>/annuller-salg", views.cancel_sale, name="cancel_sale"),
    path("tildeling/<int:pk>/koeb", views.buy, name="buy"),
    path("tildeling/<int:pk>/udeblevet", views.mark_absent, name="mark_absent"),
    path("admin", views.admin_overview, name="admin"),
    path("admin/beboer/<int:pk>", views.admin_resident, name="admin_resident"),
]
