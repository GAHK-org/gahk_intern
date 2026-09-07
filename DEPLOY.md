# Deployment runbook

Target stack (scope §4): **Django 5.2 + gunicorn** behind **nginx/Traefik**, **PostgreSQL**, **WhiteNoise**
for static, on **Hetzner** with **Coolify** (git-push deploys + Let's Encrypt TLS) or **Kamal**. MediaWiki
stays a separate **PHP + MariaDB** app on the same box. No SPA/API — one monolith.

> Items marked **[you]** need your accounts/credentials (GitHub, Hetzner, Punktum dk) — I can't do them from here.

## 1. Local
Prereqs: [`uv`](https://docs.astral.sh/uv/) (Python deps) + [`go-task`](https://taskfile.dev) + Node.
```
task install      # uv sync → ./.venv from pyproject.toml + uv.lock
task db:up        # Postgres + MariaDB (dev)
task dev          # build assets + migrate + runserver → http://127.0.0.1:8800
task test         # pytest
task lint         # ruff check + format --check
task build        # prod asset build + collectstatic
docker build -t gahk .   # full production image
```

## 2. Secrets / environment (prod → `app/.env.prod`, never committed)
All are read from the environment (F-013); rotate everything at cutover (scope §5 — legacy secrets are compromised).
```
DJANGO_SECRET_KEY=<64+ random chars>
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=www.gahk.dk,gahk.dk
DATABASE_URL=postgres://gahk:<pw>@postgres:5432/gahk
VISIT_COUNTER_HMAC_KEY=<random>
SMTP_HOST=send.one.com  SMTP_USER=…  SMTP_PASSWORD=…  SMTP_PORT=587
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
DEFAULT_FROM_EMAIL=autosvar@gahk.dk               # ⚠ must be an alias of SMTP_USER — see below
OELKAELDER_FROM_EMAIL=bierkeller@gahk.dk          # ⚠ second sender, same requirement
TURNSTILE_SITE_KEY=…  TURNSTILE_SECRET_KEY=…      # Cloudflare Turnstile [you]
WIFI_PASSWORD=…  GOOGLE_CALENDAR_USER=…  GOOGLE_CALENDAR_PASSWORD=…
INDSTILLING_EMAIL=indstillingen@gahk.dk           # recipient only, not a sender
POSTGRES_PASSWORD=<pw>                            # for the compose postgres service
OELKAELDER_KIOSK_IPS=<the till's server-observed source IP>   # confirm from access logs, NOT ipconfig
VAPID_PUBLIC_KEY=…  VAPID_PRIVATE_KEY=…           # Web Push for Den Hurtige — see below
VAPID_ADMIN_EMAIL=autosvar@gahk.dk                # real address: push services report failures here
S3_BUCKET=…  S3_LOCATION=fsn1  S3_ACCESS_KEY=…  S3_SECRET_KEY=…   # media object storage — see §4c
AWS_REQUEST_CHECKSUM_CALCULATION=when_required    # ⚠ required with the above — see §4c
AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
```
Store real values in Coolify's secret manager / a vault — not in git.

**Web Push (Den Hurtige).** The two VAPID values are the *raw base64url* key pair, not PEM files —
`VAPID_PUBLIC_KEY` is handed to the browser as `applicationServerKey`, which must be the 65-byte
uncompressed EC point. `app/.env.example` carries the generate/convert one-liner. Leave them unset
and the feature degrades cleanly: the feed still works, the subscribe button just reports that push
is not configured. **Rotating the pair invalidates every existing subscription**, so every resident
would have to press the button again — settle on one pair before residents start subscribing.
Push also requires HTTPS, which Traefik already terminates; on iOS the site must be added to the
home screen before notifications are available at all.

**Email — verify the senders, don't assume.** one.com authenticates as `SMTP_USER` and rejects any
From address that account is not itself or an alias of:
`550 5.7.1 [M9] User [it@gahk.dk] not authorized to send on behalf of <autosvar@gahk.dk>`. Both
`DEFAULT_FROM_EMAIL` and `OELKAELDER_FROM_EMAIL` must therefore be added as aliases on the
`SMTP_USER` mailbox in the one.com control panel (or pointed at `SMTP_USER` itself). Test each from
inside the running container — this is the only way to confirm, since MX stays on one.com (§7) and
the app relays through their smarthost, so existing SPF/DKIM for gahk.dk covers it with no new DNS:
```
python manage.py sendtestemail you@example.com                       # exercises DEFAULT_FROM_EMAIL
DEFAULT_FROM_EMAIL=$OELKAELDER_FROM_EMAIL python manage.py sendtestemail you@example.com
```
Exit 0 = delivered. A failure here is otherwise near-invisible in the UI: the mails are best-effort
so the app keeps working, and only the server log records the rejection. The one exception is
password reset — Django's built-in view does not suppress errors, so a bad `DEFAULT_FROM_EMAIL`
turns "glemt kodeord" into a 500 for the resident.

## 3. CI (GitHub Actions — `.github/workflows/ci.yml`) **[you: create the repo]**
Fresh repo (do **not** import the legacy history — it contains plaintext secrets, scope §5). Jobs:
`lint` (ruff + bandit), `test` (pytest against a Postgres service), `security` (pip-audit), `build`
(`docker build`). Add **Dependabot**. mypy is optional (code is currently untyped) — add later with django-stubs.

## 4. Hosting (Hetzner) **[you]**
1. Provision a Hetzner CX/CPX VM (EU/GDPR-aligned, DK-close).
2. Install **Coolify** (recommended): git-push deploys, TLS, env management. Point it at the repo; it builds
   the `Dockerfile` and runs it. (Alternative: **Kamal** — `kamal setup`/`deploy` using the same Dockerfile.)
3. Run **Postgres** (managed, or the `postgres` service in `docker-compose.prod.yml`) and **MariaDB** (for
   MediaWiki). Mount the **`media` volume** for uploads — still required, and still the rollback,
   even after the object-storage migration in §4c.
4. `web` runs `migrate` on start then gunicorn; Coolify/Traefik terminates TLS and proxies to :8000.
5. `Scaleway` is the fallback if you prefer a first-party managed Postgres.

### 4b. Scheduled tasks (Coolify → the `web` resource → **Scheduled Tasks**)

Coolify runs each of these *inside the already-running `web` container* on a cron expression, and
keeps the output in its own log view. That is why there is no cron sidecar, no host crontab (which
would fight Coolify's control of the compose lifecycle) and no Celery beat (a broker plus a worker
for four jobs a month). Container name: `web`. Every command below is **idempotent** — a double run
or an overlapping run is harmless.

| Command | Cron | Why |
| --- | --- | --- |
| `python manage.py purge_applications` | `20 3 * * *` | **The one that is genuinely missing.** F-001 says applications are kept one year; nothing has ever enforced it, so applicant PII accumulates indefinitely — the exact GDPR gap 99-index.md flags in the legacy system. |
| `python manage.py ak_monthly_assessment` | `10 4 1 * *` | Books the month's AK deduction on the 1st instead of whenever someone happens to open an internal page. |
| `python manage.py purge_quick_posts` | `*/30 * * * *` | Drains expired Den Hurtige posts (and their images) even in a quiet week when nobody loads the feed. |
| `python manage.py purge_notices` | `40 3 * * *` | Sweeps compose-toolbar images uploaded to a post nobody ever saved. **It no longer deletes opslag** — the board keeps its archive (spec/features/opslagstavle.md). The name is kept so this row and the Coolify task stay valid; if it is ever renamed, both move in the same change. Offset from `purge_applications` (03:20) so two deletes never overlap on the same small box. |
| `python manage.py archive_finished_repairs` | `50 3 * * *` | Archives (never deletes) a Reparationer ticket that has sat in Færdig for over 30 days, so the board does not fill up with old closed repairs — still searchable via the Arkiv page. Offset from `purge_notices` (03:40) so the two never overlap. |
| `python manage.py purge_events` | `0 4 * * *` | Enforces Begivenheder's retention: an event goes a week after it ends, a cancelled one thirty days after it was cancelled (two clocks, see `events/models.py`). Offset to 04:00 so it does not overlap `purge_applications` (03:20), `purge_notices` (03:40) or `archive_finished_repairs` (03:50) on the same small box. |
| `python manage.py remind_rsvp_deadlines` | `0 17 * * *` | Nudges the people who have not answered when a svarfrist falls inside the next 24 hours. **Once per event** — the claim is a compare-and-swap on `reminder_sent_at`, taken *before* the send, so a crash between the two loses one reminder rather than pushing the whole house twice. Runs at 17:00 rather than overnight because it is a notification people are meant to act on. |

**Run `purge_applications --dry-run` by hand first.** It deletes permanently and there is no undo;
confirm the count is what you expect before putting it on a schedule.
`purge_notices` needs less care: it only ever removes uploads no post references, so the number to
eyeball on the first real run is simply that unused-image count.

`purge_events` and `remind_rsvp_deadlines` both have lazy backstops in the events list view, for
the reason given below; `purge_notices` does not.

**`purge_notices` is the deliberate exception to the lazy-guard rule below, and should stay that
way.** Den Hurtige purges on every feed load because its promise is "gone in 30 minutes": a message
that should have vanished is visibly wrong to a reader within the hour, so cron failing silently has
an immediate cost. Opslagstavlen now deletes no posts at all, and its sweep affects nothing a reader
can see — a missed night leaves a few unreferenced files in the bucket. Putting that on every board
request would be a bad trade. Don't "fix" the inconsistency.

**These do not replace the lazy guards, and the lazy guards should stay.** `ensure_active_month_applied()`
costs two indexed single-row queries on three pages (measured), and it is the only thing that makes a
missed cron self-healing. Cron's failure mode is silence — a bad expression, a container restarted at
the wrong minute, a task disabled during debugging — and a skipped `ak_monthly_assessment` means every
resident's balance is quietly wrong until someone notices. The lazy check turns that into "runs a few
hours late". Belt and braces, and both paths are idempotent so they cannot double-charge.

**Timezone.** Coolify evaluates cron on the host clock (UTC on a stock Hetzner box) while Django's
`TIME_ZONE` is `Europe/Copenhagen`. Irrelevant for the daily and half-hourly jobs; it matters for the
monthly one, where `10 4 1 * *` UTC is 05:10/06:10 local on the 1st — safely inside the day either
way. Do not move it near midnight, or DST will eventually put it on the wrong side of a month
boundary.

**Deliberately not scheduled:** ølkælder interest (`apply_interest`). It is already idempotent per
calendar month and exposed as a button for the ølkælder officers, so automating it is a policy
decision for them, not a technical one.

### 4c. Media on object storage (Hetzner Object Storage)

Uploads live in an S3-compatible bucket rather than the `media` volume. **`S3_BUCKET` is the whole
switch** — there is no second flag and no half-enabled state.

**`MEDIA_URL` stays `/media/`, permanently.** It is a prefix of content stored in the *database*:
`cms.Page.background_image` is a CharField holding the URL string outright, the CMS toolbar writes
`<img src="/media/…">` into page bodies, and opslag bodies embed the same in Markdown. Repointing it
at the bucket host makes those images vanish (the sanitiser drops a src it does not recognise) and
makes the next edit of an existing opslag release its images for `purge_notices` to delete a day
later — silently, both. `core/storage.py` carries the argument; `core.checks` (**core.E007-E010**)
refuses to start the process if it is broken. `core.media.serve_media` answers `/media/<path>` with a
302 to a short-lived presigned URL instead.

#### The migration, in the order it must happen

The hazard is **`S3_BUCKET` being set when the code first deploys**. The app then boots straight onto
an empty bucket and every existing image 404s at once — the files are still on the volume, but
nothing serves them. Deploy first, migrate second, flip third:

**Stage 0.** Leave `S3_BUCKET` unset in Coolify. The other five variables are inert without it.

**Stage 1 — deploy, still on disk.** The app behaves exactly as before, so this stage validates the
two things that *did* change behaviour, with zero storage risk:
```
docker exec <web> python manage.py check                      # core.E007-E010 silent
docker exec <web> sh -c 'find /app/media -type f | wc -l'     # baseline count
docker exec <web> python manage.py audit_media --limit 5      # baseline, and note "Present"
```
Then, logged **out**: a public CMS page still shows its images, and `/media/profile_pictures/<real>`
redirects to the login page. Logged **in**: that same URL returns the image. A missing front-page
image here is the auth gate's prefix list, not storage.

**Stage 2 — migrate, app still serving from disk.** Inject the bucket for the single command:
```
docker exec -e S3_BUCKET=<bucket> <web> python manage.py migrate_media_to_s3 --dry-run
docker exec -e S3_BUCKET=<bucket> <web> python manage.py migrate_media_to_s3
docker exec -e S3_BUCKET=<bucket> <web> python manage.py audit_media --limit 5
```
The upload count must match the baseline. **This is where the botocore checksum flags get their
first real test** — and the right place for it, because a failure here is harmless: the live site is
still serving from the volume.

Then re-run `migrate_media_to_s3`. `0 file(s) uploaded, N already present and identical` is the
verification that matters: `_already_there` compares size **and** MD5 against each object's ETag, so
"identical" for every file means the bodies round-tripped intact and the checksum flags are right. A
mass re-upload means they are not.

**Stage 3 — flip.** Set `S3_BUCKET` permanently and **restart** (an env change does not reach a
running container, and Coolify then gives you a container with a *new name*). Confirm:
```
docker exec <web> python manage.py shell -c "from django.core.files.storage import storages; print(type(storages['default']).__name__)"
```
→ `MediaS3Storage`. Run `migrate_media_to_s3` once more to sweep up anything uploaded during the
window, then check images in a browser, logged out and logged in, and upload one new image.

**Rollback** is unsetting `S3_BUCKET` — **but only while the `media` volume still has the files.**
Once it is emptied (below) that path is gone, which is what `core.E010` exists to enforce.

#### Emptying the volume

Only after the bucket has versioning **and** a lifecycle rule (§4d), and the backups are green:
```
docker exec <web> python manage.py migrate_media_to_s3      # expect 0 uploaded
docker exec <web> python manage.py audit_media --limit 5    # MISSING only https://gahk.dk/... rows
docker exec <web> sh -c 'find /app/media -mindepth 1 -delete'
```
Leave the mount in `docker-compose.prod.yml`; an empty volume costs nothing.

#### Settings that are load-bearing, not tuning

Both are commented in `config/settings.py`:
- **`file_overwrite=False`** — django-storages defaults it to *True*, which skips Django's
  name-suffixing. `Resident.profile_picture` uploads to a flat prefix with no uniquifier, so the
  second resident to upload an `IMG_1234.jpg` would silently replace the first one's photo.
- **`location="media"`** — with no prefix, `safe_join` collapses `..` instead of raising, and
  `/media/../backups/…` would hand out a presigned URL for a database dump. The prefix is what makes
  one bucket safe to share with §4d.

**The two `AWS_*_CHECKSUM_*` variables are not credentials.** They are botocore behaviour flags whose
value is the literal string `when_required`; there is nothing to obtain. Without them botocore ≥1.36
sends `x-amz-checksum-crc32` with `aws-chunked` framing on every PUT, which Hetzner mis-stores or
rejects — and the upload still returns 200.

`audit_media` is **report-only and must stay that way.** A media reference can live in six places,
only one of which is a FileField; `tests/test_audit_media.py` has one test per source. A permanent
~134-row MISSING section is expected and is *not* missing files: `oelkaelder.Product.image` is a
FileField whose legacy rows hold the old site's absolute URL ("legacy imageurl"). Those images are
broken on the live site today, independently of any of this, and are a separate fix.

#### The bucket needs a CORS rule once Arkiv can upload

Arkiv sends files **straight from the browser to Hetzner** (`arkiv/uploads.py`) — a 2 GB video
cannot go through three synchronous gunicorn workers. That POST is cross-origin, from
`https://gahk.dk` to `https://<bucket>.<loc>.your-objectstorage.com`, so the bucket has to say the
origin is allowed or the browser refuses to send it.

**Nothing in dev or CI can catch a missing rule**, because the local path never leaves the app: the
symptom is uploads failing in production only, with a CORS error in the browser console and nothing
at all in the Django log.

```
docker exec <web> python manage.py shell -c "
from django.core.files.storage import storages
s = storages['default']; c = s.connection.meta.client
c.put_bucket_cors(Bucket=s.bucket_name, CORSConfiguration={'CORSRules': [
  {'AllowedOrigins': ['https://gahk.dk', 'https://www.gahk.dk'],
   'AllowedMethods': ['POST'],
   'AllowedHeaders': ['*'],
   'ExposeHeaders': ['ETag'],
   'MaxAgeSeconds': 3600},
]})
print(c.get_bucket_cors(Bucket=s.bucket_name)['CORSRules'])"
```

`POST` only, and only those origins. **Reading needs no CORS** — a download is a top-level
navigation to a redirect, not a `fetch`, so this became necessary the day upload landed and not
before. Widening `AllowedMethods` to `GET`/`PUT` or `AllowedOrigins` to `*` would let any page on
the internet script requests against the bucket with a stolen presigned URL; there is no reason to.

#### `/media/` is no longer public

It used to be, as the legacy `/public/` images were — so anyone who guessed
`/media/profile_pictures/IMG_1234.jpg` (a real name: no date, no random suffix) could read a
resident's photograph. Now only `core.media.PUBLIC_PREFIXES` — just `cms/` — is anonymous.

`cms/` is the exception because `cms.CmsImage` uploads are embedded in `Page.body`, `NewsItem.body`
and `Event.description`, which the logged-out front page renders; `body_media` rewrites only the
*legacy* `/public/…` paths to `/static/legacy/`, so it never moves these off `/media/`. Every other
prefix was checked individually and is reachable only from `/intern/` — including `oel/` and
`public/`, which look public and are not (ølkælder lives under `/intern/oelkaelder/`, and
`relocate_media` copies only ølkælder and værelsestjek legacy images into `media/public/`).

**If a front-page image ever goes missing, this list is the first thing to check.** The gate is a
blanket `login_required` per prefix, not per-object: any logged-in resident can fetch any non-`cms/`
file, so a *private* begivenhed's poster is protected from the public but not from other residents.
Closing that would mean resolving each path back to its owning row and applying
`events.access.visible_to`; deliberately not done.

### 4d. Backups, versioning and restore

Configured on the **Coolify database resources**, not as management commands: Coolify runs `pg_dump`
inside the database container, where the client version always matches the server. A
`manage.py backup_db` would need `postgresql-client` added to a `python:3.13-slim` image and pinned
in lockstep with the managed Postgres major forever — a mismatched `pg_dump` refuses to run.

| What | Schedule (UTC) | Retention | Why |
| --- | --- | --- | --- |
| **Postgres** (gahk) | `0 2 * * *` | 30 days | Residents, AK ledger, ølkælder balances, opslag |
| **MariaDB** (MediaWiki) | `20 2 * * *` | 30 days | Its own app and its own DB — easy to forget |
| **Coolify instance** | `40 2 * * *` | 14 days | Its DB holds the app definitions and every env var in §2 |

Destination is the same bucket as media, under `backups/`. That is safe because Django's storage is
pinned to `location="media"` (§4c), so `/media/../backups/…` raises rather than resolving, and
`audit_media` is prefix-scoped to `media/` and never reports a backup as an orphan.

Offsets matter: §4b already runs tasks at 03:20, 03:40, 03:50 and 04:00, so backups sit in the
02:00-02:40 window and never overlap a purge on this small box. Coolify evaluates cron on the **host
clock (UTC)** while Django's `TIME_ZONE` is `Europe/Copenhagen`.

#### Versioning and lifecycle — both, or neither is worth much

Versioning is the undo for a bad purge; `core/files.py` is otherwise the sole executioner of the sole
copy. **It is off by default on a new Hetzner bucket** — check rather than assume, and note that a
bucket without it returns no `Status` key at all:
```
docker exec <web> python manage.py shell -c "
from django.core.files.storage import storages
s = storages['default']; c = s.connection.meta.client
print(c.get_bucket_versioning(Bucket=s.bucket_name).get('Status'))"
```

**Versioning without a lifecycle rule would be actively wrong here.** Den Hurtige hard-deletes its
images after 30 minutes to 24 hours and `purge_notices` sweeps opslag images nightly; every one of
those deletions leaves a retained noncurrent version *forever*. You would pay to store precisely the
images the features promise to destroy, and quietly break a deletion promise made to residents. So:
```
c.put_bucket_lifecycle_configuration(Bucket=s.bucket_name, LifecycleConfiguration={'Rules':[
  {'ID':'expire-noncurrent-versions','Filter':{'Prefix':'media/'},'Status':'Enabled',
   'NoncurrentVersionExpiration':{'NoncurrentDays':30}},
  {'ID':'abort-incomplete-uploads','Filter':{'Prefix':''},'Status':'Enabled',
   'AbortIncompleteMultipartUpload':{'DaysAfterInitiation':7}},
]})
```
Thirty days is the undo window: long enough to notice a bad purge, short enough that the deletion
promise holds in substance. The media rule is scoped to `media/` on purpose — `backups/` wants its
own retention, not this one.

#### One copy must live off Hetzner

Same provider, same project, same credentials: an unpaid invoice, a suspension or a leaked token
takes the primary *and* the backups. A weekly `rclone` pull to a Hetzner Storage Box (different
product, different credentials) or a documented quarterly manual download both count; nothing does
not. **The Coolify instance backup contains the S3 credentials for the bucket it is written into**,
so keep those keys somewhere outside Hetzner or you can read that backup only by already having what
is in it.

Note also that a 30-day backup retention means applicant data deleted by `purge_applications`
survives up to 30 days longer than F-001's one-year policy implies. Normal and defensible; written
down here so it is a decision rather than a discovery.

#### Verifying a backup, and rehearsing a restore

A backup nobody has restored is not a backup. What landed in the bucket:
```
docker exec <web> python manage.py shell -c "
from django.core.files.storage import storages
for o in storages['default'].bucket.objects.filter(Prefix='backups/'):
    print(o.last_modified, f'{o.size/1e6:.1f} MB', o.key)"
```
A zero-byte or implausibly small dump is the classic silent failure. **Confirm an *unattended* run
appears** — a manually triggered one proves the credentials, not the schedule.

The rehearsal, against the dev Postgres container (`task db:up`), never touching the dev database:
```
docker cp <dump>.dmp ny_ny_intern-postgres-1:/tmp/d.dmp
docker exec ny_ny_intern-postgres-1 pg_restore --list /tmp/d.dmp | head        # readable? right dbname?
docker exec ny_ny_intern-postgres-1 psql -U gahk -d postgres -c "CREATE DATABASE gahk_restore_test;"
docker exec ny_ny_intern-postgres-1 pg_restore -U gahk -d gahk_restore_test --no-owner --no-privileges /tmp/d.dmp
docker exec ny_ny_intern-postgres-1 psql -U gahk -d gahk_restore_test -c   "SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE n_live_tup>0 ORDER BY 2 DESC LIMIT 20;"
docker exec ny_ny_intern-postgres-1 psql -U gahk -d postgres -c "DROP DATABASE gahk_restore_test;"
```
Exit 0 with no errors, and row counts in the right order of magnitude. A healthy dump is ~4 MB for
roughly 390k rows across 63 tables, dominated by the ølkælder ledger; ~124 residents. The database
holds no binary data now that media is in the bucket. Cross-check the applied migrations against the
branch — every project migration should be present (Django's own built-ins add ~18 beyond the files
under `app/*/migrations/`).

## 5. Data migration to prod
```
mysqldump the live gahk_dk  →  load into the MariaDB staging container
task etl          # seed_rooms + all etl_* + reset_sequences   (needs LEGACY_DB_* env pointing at staging)
task etl:verify   # count diff legacy ↔ clean (expect residents/residencies/kvotient/deposits lower; rest equal)
python manage.py relocate_media   # copy referenced legacy images into MEDIA_ROOT
```
Encoding: connection charset is utf8mb3 — check per-table charsets, watch latin1→utf8 mojibake (02-schema-etl §1.4/A2).
**Carve out the `wiki*`-prefixed tables** — they belong to MediaWiki (MariaDB), not the Postgres ETL.

## 6. MediaWiki workstream (keep, don't rewrite — scope §3) **[you]**
- It currently **shares the `gahk_dk` DB** (user `gahk_dk`, table prefix `wiki`; `LocalSettings.php`).
  Split the `wiki*` tables into their **own MariaDB database** on the new box.
- **Upgrade to the current LTS (1.43, supported to Dec 2027)**. MediaWiki upgrades span at most two LTS
  releases at a time, so if the current install is old this is **multi-step**; then stay on the LTS track.
- Serve behind the same proxy at `wiki.gahk.dk` or `/wiki`. Confirm open Q: is wiki login standalone or
  SSO'd to the members area? (Separate `wiki`-prefixed user tables imply standalone; verify LocalSettings.)

## 7. DNS cutover (.dk via Punktum dk) **[you]**
1. A **day ahead**, lower the A-record TTL (e.g. 300s).
2. Repoint the **A record** to the Hetzner IP. **Leave MX on one.com** (email untouched).
3. Serve **301 redirects** for any changed URLs to protect SEO. The rewrite **preserves** the public URLs
   (`/`, `/faciliteter`, `/faciliteter/vaerelse`, `/kollegielivet/historie`, `/optagelse`, `/pylon`→ n/a, `/wiki`).
4. **Rollback** = repoint the A record back. Registrar transfer is optional/separate (MitID + auth code).
5. Confirm **who the registrant of record is** early — often the slowest item.

## 8. Cutover checklist
- [ ] Fresh GitHub repo, CI green, Dependabot on.
- [ ] Hetzner + Coolify up; `.env.prod` secrets set (all rotated).
- [ ] Postgres + MariaDB provisioned; `media` volume mounted.
- [ ] **Media on the bucket (§4c):** bucket in `fsn1` with a dot-free name; `S3_BUCKET` left UNSET
      for the first deploy; stages 1-3 followed in order; the re-run of `migrate_media_to_s3`
      reporting `0 uploaded, N already present and identical` (the checksum proof).
- [ ] **Before residents can upload to Arkiv:** the bucket CORS rule set (§4c) and one real upload
      tried from a browser — a missing rule fails in production only, and silently in the Django log.
- [ ] **Before emptying the `media` volume:** bucket versioning `Enabled` **and** the lifecycle rules
      in place (§4d), backups green, and one copy off Hetzner. Until then the volume is the rollback.
- [ ] After the bucket is live, confirm images still render on a **public CMS page** while logged
      OUT — that is the live, ETL-populated content, the only prefix the gate leaves anonymous, and
      the one that actually has something to lose. Then log in and check opslagstavlen and
      begivenheder. All of these fail silently: the page returns 200 either way.
- [ ] `task etl` + `etl_verify` + `relocate_media` run against a fresh dump; counts sane.
- [ ] `createsuperuser`; assign initial roles via `/admin/roles`.
- [ ] `sendtestemail` green for **both** sender addresses (§2); one real "glemt kodeord" round-trip.
- [ ] Scheduled tasks created in Coolify (§4b); `purge_applications --dry-run` reviewed before the
      daily job is enabled, and each task run once by hand so its log is known-good.
- [ ] Backups configured on all three Coolify resources (§4d); an **unattended** run confirmed in
      the bucket, and one restore rehearsed end-to-end with row counts checked.
- [ ] Interim hardening done on the *old* box until DNS flips (scope §8: lock KCFinder, gate mass-mailers,
      delete `phpinfo.php`, pull & archive access logs).
- [ ] MediaWiki migrated + upgraded to 1.43, reachable behind the proxy.
- [ ] Staging smoke-tested; behavioural diff vs the live PHP site on key pages.
- [ ] Lower A TTL → repoint A → verify → keep MX on one.com.
- [ ] 301s live for any changed URLs.

## 9. Notes
- Python: **dev is 3.14, prod image pins 3.13** (Django 5.2's supported range; psycopg wheels).
- Static: WhiteNoise `CompressedManifestStaticFilesStorage` in prod (hashed/cached); `collectstatic` runs at image build.
- Fonts (Julius Sans One / Ubuntu) currently load from Google Fonts — self-host for a fully CSP-clean/offline prod.
- The `gahk_dk` DB account is shared by App A, App B, and MediaWiki — rotating its password touches all three.
