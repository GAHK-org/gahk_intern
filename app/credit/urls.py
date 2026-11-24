from django.urls import path

from . import views

app_name = "credit"

urlpatterns = [
    path("", views.index, name="index"),
    path("transfer/", views.transfer, name="transfer"),
    path("accounts/<int:pk>/", views.account, name="account"),
    path("accounts/<int:pk>/limit", views.set_limit, name="set_limit"),
    path("accounts/<int:pk>/close", views.close, name="close"),
    path("transfers/<int:pk>/reverse", views.reverse, name="reverse"),
    path("books/", views.books, name="books"),
    path("books/<int:pk>/", views.book, name="book"),
    path("books/<int:pk>/charge", views.charge, name="charge"),
    path("books/<int:pk>/accounts/new", views.open_account, name="open_account"),
]
