from django.urls import path

from . import views

app_name = "oelkaelder"

urlpatterns = [
    path("", views.shop, name="shop"),
    path("purchase", views.purchase, name="purchase"),
    path("min-saldo", views.my_balance, name="my"),
    path("admin", views.admin, name="admin"),
    path("deposit/<int:pk>", views.add_deposit, name="deposit"),
    path("deposit/<int:pk>/annuller", views.void_deposit, name="void_deposit"),
    path("admin/deaktiver/<int:pk>", views.deactivate_shopper, name="deactivate_shopper"),
    path("admin/genaktiver", views.activate_shopper, name="activate_shopper"),
    path("admin/tilfoej", views.add_shopper, name="add_shopper"),
    path("admin/advarsel/<int:pk>", views.update_warning, name="update_warning"),
    path("admin/rente", views.update_interest, name="update_interest"),
    path("admin/koebsgraense", views.update_purchase_policy, name="update_purchase_policy"),
    path("admin/rente/anvend", views.apply_interest_view, name="apply_interest"),
    path("admin/rapport/indbetalinger", views.report_deposits, name="report_deposits"),
    path("admin/rapport/salg", views.report_sales, name="report_sales"),
    path("admin/rapport/antal", views.report_quantity, name="report_quantity"),
    path("admin/salgsoverblik", views.all_sales, name="all_sales"),
    path("admin/salgsoverblik/<int:pk>/annuller", views.void_sale, name="void_sale"),
    path("admin/person", views.person_history, name="person_history"),
    path("admin/person/<int:pk>/justering", views.add_adjustment, name="add_adjustment"),
    path("admin/justering/<int:pk>/annuller", views.void_adjustment_view, name="void_adjustment"),
    path("admin/produkter", views.products, name="products"),
    path("admin/produkter/opret", views.product_create, name="product_create"),
    path("admin/produkter/<int:pk>", views.product_update, name="product_update"),
]
