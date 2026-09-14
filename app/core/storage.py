"""The media storage backend, and the one invariant the whole S3 migration rests on.

    MEDIA_URL IS PART OF THE DATABASE. IT CANNOT BE CHANGED.

That sentence is the entire reason this module exists, so it is worth being precise about why.

A media URL is not only a way to *reach* a file here — in several places it has been written into
the database as data. `cms.Page.background_image` is a *CharField holding the URL string itself*,
not a FileField name, and the CMS toolbar writes `<img src="/media/...">` straight into page bodies.
Resident-authored Markdown in `opslagstavle.Notice.body` embeds the same way.

Weight those correctly. **The CMS half is live** — it is the public site, populated by the legacy
ETL, and it breaks the day MEDIA_URL moves. Opslagstavlen is still gated to Inspektionen and
Netværksgruppen (`opslagstavle/access.py`), so it holds a trial's worth of posts, not an archive;
its exposure is small *today* and becomes the larger one the moment that gate opens, which is the
point of writing this down rather than relying on "there is hardly anything in it".

Four pieces of code treat `settings.MEDIA_URL` as a string prefix of that stored content:

  * core.markdown._LOCAL_IMAGE_PREFIXES — nh3's attribute filter DROPS any <img src> that does not
    start with it, and _SRCLESS_IMG then removes the tag outright;
  * core.markdown.extract_image_names — strips the prefix to recover FileField names for the indexed
    `file__in=` lookup that opslagstavle.images.sync_images claims images with;
  * cms.admin.CmsImageAdmin.usage — `Page.objects.filter(background_image=obj.url)`, an exact match;
  * rooms.RoomConditionScore.image_urls — concatenates MEDIA_URL onto legacy `;`-separated paths.

So pointing MEDIA_URL at the bucket host — the obvious way to serve media from S3, and what
django-storages does by default — fails like this, with nothing in any log:

  1. cms.admin's usage column reports "Ingen steder endnu" for images that are live on the front
     page, so the next editor to tidy up deletes one. A new background saved after the move stores
     a presigned URL, which expires within the hour and is then wrong permanently.
  2. Every existing opslag image disappears, because the stored `/media/…` src no longer matches the
     new prefix and the sanitizer strips it.
  3. extract_image_names() returns an empty set for every pre-existing post, so the next edit of one
     RELEASES all of its images, and purge_notices deletes the files a day later.

None of that raises. That is why the fix is not "remember not to do it" but this class plus the
system checks in core.checks (core.E007/E008), which refuse to start the process.

THE DESIGN: /media/ stays a stable, app-owned URL space forever. `url()` returns the same
site-relative path FileSystemStorage always returned, so nothing that reads or writes a URL string
notices the backend changed. The real presigned URL is reachable through `signed_url()`, which only
core.media.serve_media calls — it 302s /media/<path> to a short-lived signature.

The cost is one Django request per file. That is not a regression: config/urls.py has always routed
every /media/ hit through django.views.static.serve, which additionally *streams the bytes*. A 302 is
pure local HMAC — generate_presigned_url makes no network call — so this is strictly cheaper than
what it replaces.
"""

from typing import Any
from urllib.parse import urljoin

from django.conf import settings
from django.utils.encoding import filepath_to_uri
from storages.backends.s3 import S3Storage

# The key prefix every media object lives under, and the reason it is not optional.
#
# django-storages resolves a name with `safe_join(self.location, name)`. With a location set, a name
# that climbs out of it raises SuspiciousOperation. With location EMPTY there is nothing to climb out
# of, and safe_join quietly collapses the dots instead: "../../etc/passwd" normalises to
# "etc/passwd", and the storage happily signs a URL for it.
#
# /media/<path> is public and unauthenticated, so with no prefix a request for
# `/media/../backups/db/2026-09-04.dump` would return a 302 to a working presigned URL for the
# database dump — in a bucket that, by design (see DEPLOY.md), also holds the backups. The prefix is
# what makes one bucket safe to share.
MEDIA_PREFIX = "media"


class MediaS3Storage(S3Storage):
    """S3 for the bytes, `/media/<name>` for the URL. See the module docstring.

    Set as STORAGES["default"] only when S3_BUCKET is configured; dev and CI run on plain
    FileSystemStorage, and the two must produce byte-identical URLs for that to be safe.
    """

    def get_default_settings(self) -> dict[str, Any]:
        """Default `location` to MEDIA_PREFIX rather than django-storages' empty string.

        Defaulted here rather than only set in config/settings.py because an empty location is not a
        tidiness problem, it is a bucket-wide read primitive — see MEDIA_PREFIX above. core.checks
        (core.E009) additionally rejects an explicit empty one, since a default cannot stop somebody
        writing `"location": ""` into OPTIONS.
        """
        return {**super().get_default_settings(), "location": MEDIA_PREFIX}

    def url(
        self,
        name: str,
        parameters: dict | None = None,
        expire: int | None = None,
        http_method: str | None = None,
    ) -> str:
        """The site-relative media URL, reproducing FileSystemStorage.url() exactly.

        NOT f"{settings.MEDIA_URL}{name}". Django's implementation quotes the name with
        filepath_to_uri and joins with urljoin, and the difference is load-bearing rather than
        pedantic: extract_image_names recovers the storage name with a naive `src[len(prefix):]` and
        never unquotes, so a percent-encoding mismatch here yields names that miss the `file__in=`
        lookup — which silently releases every image on the post's next edit. Reproduce, don't
        reimplement.

        `parameters`/`expire`/`http_method` are accepted to keep the Storage interface intact and
        deliberately ignored: they only mean anything for a presigned URL, and callers who want one
        must ask for it by name via signed_url().
        """
        url = filepath_to_uri(name)
        if url is not None:
            url = url.lstrip("/")
        return urljoin(settings.MEDIA_URL, url)

    def signed_url(self, name: str, expire: int | None = None) -> str:
        """A short-lived presigned GET, straight from django-storages.

        The only caller is core.media.serve_media. Named rather than reached through url() so that
        every place a presigned URL could leak into stored content is a grep away — a signature
        written into a Notice body or into Page.background_image expires within the hour and is then
        wrong forever.
        """
        return super().url(name, expire=expire)
