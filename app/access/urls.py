from django.urls import path

from . import views
from .features import Feature, in_feature

app_name = "access"

urlpatterns = [
    path("", views.index, name="index"),
    path("person/<int:pk>", views.person, name="person"),
    # Anyone's own: why they can open what they can.
    path("mine", in_feature(Feature.ACCOUNT, views.mine), name="mine"),
    path("grants/<int:pk>/add", views.grant_add, name="grant_add"),
    path("grants/<int:pk>/end", views.grant_end, name="grant_end"),
    path("kitchen-staff/add", views.kitchen_staff_add, name="kitchen_staff_add"),
]
