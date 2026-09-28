"""Mounted at /intern/koekken/ from residents/urls.py -- global names, not namespaced there, but
this module has its own `app_name` so callers always use `{% url 'koekken:...' %}`, mirroring
reparationer/urls.py exactly. The kitchen tablet at /idag/ is the one pair of routes under here that
is unauthenticated (IP-gated instead) -- see koekken.views' module docstring.
"""

from django.urls import path

from . import views

app_name = "koekken"

urlpatterns = [
    path("", views.index, name="index"),
    path("praeferencer", views.praeferencer, name="praeferencer"),
    path("praeferencer/forklaring", views.praeferencer_forklaring, name="praeferencer_forklaring"),
    path("vagt/<int:pk>/anmeld", views.flag_vagt, name="flag_vagt"),
    path("gruppe/", views.gruppe, name="gruppe"),
    path("gruppe/anmeldelse/<int:pk>/opretholdt", views.flag_opretholdt, name="flag_opretholdt"),
    path("gruppe/anmeldelse/<int:pk>/afvist", views.flag_afvist, name="flag_afvist"),
    path("gruppe/allokering", views.allokering, name="allokering"),
    path("gruppe/override", views.override_assign, name="override_assign"),
    path("gruppe/override/<int:pk>/fjern", views.override_remove, name="override_remove"),
    path("regnskab/", views.balance_export, name="balance_export"),
    path("idag/", views.idag, name="idag"),
    path("idag/<int:pk>/marker", views.marker_udfoert, name="marker_udfoert"),
]
