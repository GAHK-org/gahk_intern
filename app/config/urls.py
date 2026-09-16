"""Root URLconf. Preserves the legacy public URLs (`/`, Danish slugs incl. multi-segment ones like
`faciliteter/vaerelse`) for SEO. Django's own admin is moved to /django-admin/ so the legacy public-site
admin can keep /admin later (F-002)."""

from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import RedirectView, TemplateView

from cms import views as cms_views
from core.media import serve_media
from events import views as events_views
from photo_album.views import serve_local_media

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("intern/", include("residents.urls")),
    re_path(r"^nyintern/(?P<rest>.*)$", RedirectView.as_view(url="/intern/%(rest)s", permanent=True)),
    path("optagelse/", include("admissions.urls")),
    path("admin/", include("residents.urls_admin")),  # legacy public-site admin (F-002)
    path("", cms_views.home, name="home"),
    path("begivenheder/", cms_views.events_news, name="events_news"),
    # The subscribable calendar feed, mounted at the ROOT rather than under /intern/ and carrying no
    # auth decorator — the token in the path is the credential (see events.views.calendar_feed).
    #
    # Root-mounted on purpose: if /intern/ is ever gated at the proxy, or somebody adds Django's
    # LoginRequiredMiddleware, a feed underneath it dies SILENTLY. Calendar clients do not report a
    # failed refresh; they just stop updating, which is the worst failure this feature can have.
    path("kalender/<str:token>.ics", events_views.calendar_feed, name="events_feed"),
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
    # images were; now only core.media.PUBLIC_PREFIXES ("cms/", the images the logged-out front page
    # embeds) is anonymous and everything else needs a session. Note ølkælder is NOT public-site
    # content despite the name — it lives under /intern/oelkaelder/.
    re_path(r"^media/(?P<path>.*)$", serve_media, name="media"),
    # photo_album's dev/CI fallback for when there is no bucket — see photo_album.storage and
    # photo_album.views.serve_local_media. In production photo_album_storage.url() returns a
    # presigned bucket URL directly, so this route is never linked to; it only exists so the
    # feature works the same way against local disk.
    #
    # `path` captures "photo-album/…" WHOLE, unlike the /media/ route above: the storage name
    # FileField.upload_to produces already carries that prefix (see photo_album/models.py), and the
    # local storage's base_url is "/" — not "/photo-album/" — precisely so the name and the URL stay
    # identical. Consuming "photo-album/" as a literal route prefix would strip it from `path` and
    # every lookup on local disk would 404 against a name that is missing its first segment.
    re_path(r"^(?P<path>photo-album/.*)$", serve_local_media, name="photo_album_media"),
    # PWA service worker for Den Hurtige. Must be served from the ROOT path: a service worker's
    # default scope is its own directory, so only a root-scoped worker covers /intern/. Served via
    # TemplateView because static/ would put it under /static/ and cap its scope there.
    path(
        "sw.js",
        TemplateView.as_view(template_name="sw.js", content_type="application/javascript"),
        name="pwa_service_worker",
    ),
    # catch-all CMS page lookup by (possibly multi-segment) slug — must stay last
    re_path(r"^(?P<url_path>[\w/-]+?)/?$", cms_views.page, name="page"),
]
