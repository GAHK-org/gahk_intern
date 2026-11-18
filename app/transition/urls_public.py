"""The URLconf of TRANSITION_PUBLIC_HOSTS (gahk.dk, www.gahk.dk) while this app still answers them.
TEMPORARY: see transition.views and DEPLOY.md, "Transition".

The old public site, plus what gahk.dk will answer after the DNS switch (gahk_web's routing.py and
.htaccess), in the same order of precedence. /health and /healthz are redirected before this, in
transition.middleware.PublicSiteHost. /static/ is WhiteNoise's, as on every host; only the old
pictures, which are no longer files here, reach the view below.
"""

from django.urls import path, re_path
from django.views.generic import TemplateView

from . import views

urlpatterns = [
    # The intranet's old homes: a page for GET/HEAD (the old installed app), else 308, prefix dropped.
    re_path(r"^(?:ny)?intern(?:/(?P<rest>.*))?$", views.intern_moved),
    # The intranet's tools and files, at the same path on intern.gahk.dk (308).
    re_path(r"^(?:admin|django-admin)(?:/.*)?$", views.to_intern),
    re_path(r"^media/(?P<path>cms/.*)$", views.library_image),
    re_path(r"^(?:photo-album|media)/.*$", views.to_intern),
    re_path(r"^(?P<target>optagelse/(?:listansoegninger|eksport))/?$", views.to_intern_path),
    re_path(r"^optagelse/(?:showAnsoegning|setasreceived|kasser)/.+$", views.to_intern),
    re_path(r"^wiki(?:/(?P<rest>.*))?$", views.to_wiki),
    # Served here as before: calendar feeds handed out with a gahk.dk address, and the service worker
    # the old installed app on gahk.dk runs (its pushes keep arriving until the DNS switch).
    path("kalender/<str:token>.ics", views.calendar_feed),
    path("sw.js", TemplateView.as_view(template_name="sw.js", content_type="application/javascript")),
    # The old site's pictures, now in the image library.
    re_path(r"^static/legacy/image/(?P<rest>.+)$", views.legacy_image),
    re_path(r"^public/image/(?P<rest>.+)$", views.legacy_image),
    # The old public site.
    path("", views.home, name="transition_home"),
    path("begivenheder/", views.events_news, name="transition_events_news"),
    path("optagelse/", views.admissions, name="transition_admissions"),
    path("optagelse/ansoeg", views.apply_for_tour, name="transition_apply_tour"),
    path("optagelse/send_rundvisning", views.apply_for_tour, name="transition_send_tour"),
    path("optagelse/fremlej", views.apply_for_sublet, name="transition_apply_sublet"),
    path("optagelse/send_fremleje", views.apply_for_sublet, name="transition_send_sublet"),
    path("optagelse/ansoeg/success", views.application_sent, name="transition_application_sent"),
    # A page by its (possibly multi-segment) address. Last.
    re_path(r"^(?P<url_path>[\w/-]+?)/?$", views.page),
]
