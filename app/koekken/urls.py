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
    path("abonner", views.save_subscription, name="save_subscription"),
    path("vagt/<int:pk>/anmeld", views.flag_vagt, name="flag_vagt"),
    path("vagt/<int:pk>/tilbyd", views.tilbyd_vagt, name="tilbyd_vagt"),
    path("bytte/<int:pk>/traek-tilbage", views.traek_tilbud_tilbage, name="traek_tilbud_tilbage"),
    path("bytte/<int:pk>/tag", views.tag_vagt, name="tag_vagt"),
    path("bytte/<int:pk>/tag-hele", views.tag_hele_vagten, name="tag_hele_vagten"),
    path("bytte/<int:pk>/foreslaa", views.foreslaa_bytte, name="foreslaa_bytte"),
    path("forslag/<int:pk>/accepter", views.accepter_forslag, name="accepter_forslag"),
    path("forslag/<int:pk>/afvis", views.afvis_forslag, name="afvis_forslag"),
    path("forslag/<int:pk>/traek-tilbage", views.traek_forslag_tilbage, name="traek_forslag_tilbage"),
    path("sommer/", views.sommer, name="sommer"),
    path("sommer/vagt/<int:pk>/tag", views.tag_sommervagt, name="tag_sommervagt"),
    path("sommer/fravaer", views.fravaer_tilfoej, name="fravaer_tilfoej"),
    path("sommer/fravaer/<int:pk>/slet", views.fravaer_slet, name="fravaer_slet"),
    path("gruppe/", views.gruppe, name="gruppe"),
    path("gruppe/anmeldelse/<int:pk>/opretholdt", views.flag_opretholdt, name="flag_opretholdt"),
    path("gruppe/anmeldelse/<int:pk>/afvist", views.flag_afvist, name="flag_afvist"),
    path("gruppe/allokering", views.allokering, name="allokering"),
    path("gruppe/festkredit/", views.festkredit, name="festkredit"),
    path("gruppe/festkredit/<int:pk>/fortryd", views.festkredit_fortryd, name="festkredit_fortryd"),
    path("gruppe/override", views.override_assign, name="override_assign"),
    path("gruppe/override/<int:pk>/fjern", views.override_remove, name="override_remove"),
    path("regnskab/", views.balance_export, name="balance_export"),
    path("idag/", views.idag, name="idag"),
    path("idag/<int:pk>/marker", views.marker_udfoert, name="marker_udfoert"),
]
