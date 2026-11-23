"""Root URLconf of intern.gahk.dk: the intranet lives at the ROOT of its own host.

The public site (gahk.dk) is served by gahk_web, a separate app on separate hosting; nothing here
renders it any more. The intranet used to sit under gahk.dk/intern/, and before that /nyintern/; here
neither prefix means anything and both answer 404 like any unknown path (gahk.dk forwards its own old
/intern/<rest> links to intern.gahk.dk/<rest>). Django's own admin is at /django-admin/ so /admin/
can hold the site-admin tools.
"""

from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import TemplateView

from access.policy import Feature, in_feature
from core.media import serve_media
from events import views as events_views
from photo_album.views import serve_media as serve_photo_album_media

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("optagelse/", include("admissions.urls")),
    path("admin/", include("residents.urls_admin")),  # site-admin tools (roles, worker jobs, …)
    # The editor for gahk.dk's content (cms). gahk.dk's "Rediger siden" links to hjemmeside/sider/<id>/.
    path("hjemmeside/", include("cms.urls")),
    # The subscribable calendar feed carries no auth decorator — the token in the path is the
    # credential (see events.views.calendar_feed). gahk.dk passes its old /kalender/ URLs through
    # to this one, so feeds handed out before the move keep refreshing.
    #
    # Mounted before the intranet on purpose: if somebody adds Django's LoginRequiredMiddleware or a
    # gate in front of the intranet, a feed underneath it dies SILENTLY. Calendar clients do not
    # report a failed refresh; they just stop updating, which is the worst failure this feature can
    # have.
    path(
        "kalender/<str:token>.ics", in_feature(Feature.PUBLIC, events_views.calendar_feed), name="events_feed"
    ),
    # User-uploaded media. Routed through Django in EVERY environment, and the URL space is fixed
    # forever: MEDIA_URL is a prefix of content stored in the database (opslag Markdown bodies,
    # cms.Page.background_image), so /media/ cannot move without a data migration — core/storage.py
    # has the full argument and core.checks refuses to start the process if it ever does.
    #
    # core.media.serve_media resolves the path against whatever STORAGES["default"] is: a 302 to a
    # presigned URL when the bytes live in object storage, a streamed FileResponse when they are on
    # local disk (dev, CI, and prod before the migration). WhiteNoise handles only *static*, and
    # DEBUG-only serving would 404 these in prod, so this route is unconditional.
    #
    # It is also where uploads are gated. /media/ used to be public by URL, as the legacy /public/
    # images were; now only core.media.PUBLIC_PREFIXES (the ølkælder till's product photos) is
    # anonymous and everything else needs a session — the website's images included, since gahk.dk
    # serves its own copies of them.
    re_path(r"^media/(?P<path>.*)$", in_feature(Feature.FILES, serve_media), name="media"),
    # photo_album's own media route (photo_album.storage.PhotoAlbumS3Storage.url() points here) —
    # a presigned bucket URL is a bearer token good for an hour with no further check, and
    # photo_album.access has a rule one can't enforce (a pending upload is visible only to its
    # uploader and Fotogruppen), so this re-checks that rule on every request instead of handing the
    # signed URL out once at render time. See photo_album.views.serve_media.
    #
    # `path` captures "photo-album/…" WHOLE rather than consuming it as a literal prefix: the
    # storage name (FileField.upload_to) already starts with "photo-album/", and url() is just "/"
    # + that name, so the route has to match on the same terms or `path` would be missing its first
    # segment and every lookup by key would miss.
    re_path(
        r"^(?P<path>photo-album/.*)$",
        in_feature(Feature.PHOTO_ALBUM, serve_photo_album_media),
        name="photo_album_media",
    ),
    # PWA service worker for Den Hurtige. Must be served from the ROOT path: a service worker's
    # default scope is its own directory, so only a root-scoped worker covers the whole site. Served via
    # TemplateView because static/ would put it under /static/ and cap its scope there.
    path(
        "sw.js",
        in_feature(
            Feature.PUBLIC, TemplateView.as_view(template_name="sw.js", content_type="application/javascript")
        ),
        name="pwa_service_worker",
    ),
    # How to install the intranet as the home-screen app GAHK Intern. Public, with no login: the old
    # app on gahk.dk sends people here, and its session stayed behind on that origin.
    path(
        "install",
        in_feature(Feature.PUBLIC, TemplateView.as_view(template_name="install.html")),
        name="install",
    ),
    # The intranet itself, at the root. Last, so the fixed prefixes above keep resolving first.
    path("", include("residents.urls")),
]
