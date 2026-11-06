# 02 — Clean Schema & ETL Design

Target schema for the Django rewrite + the migration plan from the legacy MySQL `gahk_dk` dump.
Source of truth for *what* to build: the Phase 2 specs (`spec/features/`) and `99-index.md`; source
of truth for *current behaviour/risks*: `00-manifest.md`, `01-infrastructure.md`.

> **Status:** in progress, built in domain batches (like Phase 2). **This file currently covers the
> global design + Batch 1 (core lookups, Residents & Auth, Admissions).** Remaining domains are listed
> in [§7](#7-remaining-domain-batches). Schema is expressed as **Django models** (the canonical schema
> in this stack — migrations generate the Postgres DDL); the ETL maps the legacy dump into them.

---

## 1. Global design decisions

These apply to every domain. **Flag any you want changed before I build more.**

1. **Schema = Django models; migrations generate the DDL.** We do *not* hand-write Postgres DDL.
   Target: Django 5.2 / PostgreSQL (scope §4).
2. **Only the live App A domain is modeled** — the ~36 App-relevant tables from `99-index.md` §2.
   **Not migrated to Postgres:** all 64 `wiki*` tables (MediaWiki stays on **MariaDB**, scope §3/§6),
   the App B / legacy / dead tables (`99-index.md` §5), and tables replaced by framework features (§2.4).
3. **Preserve legacy integer PKs through the ETL**, then bump Postgres sequences past `max(id)`. Keeps
   FK remapping trivial and keeps any existing internal links valid. New rows get fresh sequence ids.
4. **Encoding is per-table.** `intern_alumne`/`_liste` are `utf8mb3`; `gahk_admin_user`,
   `gahk_ansoegninger`, `gahk_news` are `latin1` (`01-infra` A2). The extract transcodes each table from
   its declared charset → UTF-8 and repairs mojibake (double-encoded Danish `æøå`). Target is UTF-8.
5. **Type modernisation (uniform rules):**
   - epoch `int` / split `day,month,year` columns → a single timezone-aware `DateTimeField`
     (`Europe/Copenhagen`).
   - `tinyint(1)` → `BooleanField`. Money (øl-domain) → **integer øre** (`PositiveIntegerField`), never float.
   - `monthNumber` (legacy encoding `12*year + month`, per `intern/delt.php`) → decoded `year` +
     `month` (or a `PeriodField`-style pair). The decode is `m = mn % 12 or 12; y = (mn-1)//12`.
   - `text` columns that are really short → `CharField(max_length=…)`; genuine free text → `TextField`.
6. **Auth is consolidated (fixes `01-infra` A4/A5, F-002/F-014).** `intern_alumne` is today *both* the
   resident directory and the login principal, with admin-ness implied by a `gahk_admin_user` join and
   seven boolean role columns. Clean model:
   - one **custom user model `Resident`** (`AUTH_USER_MODEL`), `USERNAME_FIELD = "email"`;
   - the seven role flags → **Django `Group`s** (one group per role); membership replaces the booleans;
   - passwords via a **legacy SHA-256 hasher that upgrades on next login** (scope §5) — no forced reset.
7. **Replaced by framework features (dropped, not modeled):**
   - `intern_alumne_sessions`, `intern_alumne_sessions_old` → Django sessions.
   - `intern_forgotpassword` → Django's signed, expiring, single-use password-reset tokens
     (fixes the F-014 reset findings).
   - `gahk_ansoegninger_paamindelse` → a real scheduled task if the reminder is re-enabled (it's
     currently disabled — F-000/F-001); **not migrated**.
   - `gahk_counter` (per-IP) → **not migrated** — it is operational dedup state for the old `counter()`;
     the new visit-count middleware starts fresh. `gahk_counterdato` (per-date aggregate) → **migrated**
     to `stats.DailyVisitCount` so the F-012 visitor chart keeps its history. *(Decided 2026-06: GDPR is
     not a concern for this project.)*
8. **Add the integrity the MyISAM schema lacks:** real PKs, FKs with `on_delete` policies, `unique`
   constraints (e.g. `Resident.email`), `NOT NULL` where the data supports it, and **DB-level
   transactions** (InnoDB→Postgres) so the non-atomic money/multi-row writes flagged in F-003/F-004/F-005
   become atomic.
9. **ETL is re-runnable (idempotent) and verifiable.** Every load is keyed on preserved PKs (upsert),
   and a **diff harness** compares row counts + sampled records old-vs-new (ties into scope §10's
   "diff against the running old site"). Validation failures (orphan FKs, dupes, unparseable dates)
   are reported, not silently dropped.

### Proposed Django app layout
| App | Domain | Features |
|---|---|---|
| `core` | shared lookups (Room, Workgroup, Cleaning, StudyProgramme), the legacy hasher, base mixins | cross-cutting |
| `residents` | `Resident` (user), residency/month membership, roles | F-010, F-011, auth (F-014) |
| `admissions` | tour/sublet applications | F-001, F-002 |
| `cms` | pages, news, pylon calendar | F-006, F-007, F-008 |
| `ak` | duty/krydser tracking | F-009 |
| `rooms` | room-application lottery (kvotient) + condition inspection | F-004, F-005 |
| `oelkaelder` | beer-cellar POS | F-003 |
| `stats` | (optional) visitor/admission stats | F-012 |

---

## 2. Drop / not-migrated list (decisions)

| Legacy table(s) | Disposition | Why |
|---|---|---|
| `wiki*` (64) | Stay on MariaDB | Separate MediaWiki app (scope §3) |
| `intern_alumne_sessions`, `_sessions_old` | Drop → Django sessions | framework feature |
| `intern_forgotpassword` | Drop → Django password reset | framework feature; fixes F-014 |
| `gahk_ansoegninger_paamindelse` | Drop → scheduled task | reminder disabled (F-000) |
| `gahk_counter` | Not migrated | per-IP operational dedup state; new middleware recreates it |
| `gahk_counterdato` | Migrate → `stats.DailyVisitCount` | preserves F-012 chart history |
| `intern_alumne_macaddress`, `_macaddress_temp` | Drop | MAC feature retired (2026-06) |
| `intern_handbook`, `_access`, `intern_forbrug`, `intern_alumne_pylon`, `intern_kvotient`, `intern_kvotient_counter`, `kvotient_ansoegninger`, `kvotient_counter` | Drop (App B) | App B retired; no live feature reads them |
| `*_backup`, `gahk_archive`, `jubilaeum2008`, `banned_ips` | Archive from dump, don't model | backups / one-offs / App B |
| `intern_oelkaelder_individual_price` | **Confirm** then likely drop | not touched by F-003 (open Q) |

---

## 3. Migration dependency order (FK topological)

Load in this order so FK targets exist (Batch-1 portion in **bold**):

1. **`core.Room`, `core.Workgroup`, `core.Cleaning`** (lookups; Room seeded from `delt.php`, not a table)
2. **`residents.Resident`** (+ auth `Group`s seeded; password hashes imported)
3. **`residents.Residency`** (FK → Resident, Room, Workgroup, Cleaning)
4. **`admissions.Application`** (FK → Resident for `received_by`)
5. *(later)* `cms.*`, `ak.*`, `rooms.kvotient.*`, `rooms.condition.*`, `oelkaelder.*`

---

## 4. Batch 1 — Core lookups

No legacy *table* exists for rooms; the room map is hard-coded in `intern/delt.php` (`$roomDescription`,
`$room_number`, floors/sides, indices 0–61). Seed `Room` from it. `Workgroup`/`Cleaning` come from the
lookup tables `intern_alumne_workgroup` / `intern_alumne_cleaning` (directory batch will also use them).

```python
# core/models.py
class Room(models.Model):
    # legacy "vaerelse_id" 0..61 index AND the 3-digit room number coexist in the old code (delt.php);
    # we keep both so kvotient (vaerelse_id) and the directory (room number) both map cleanly.
    legacy_index = models.PositiveSmallIntegerField(
        unique=True
    )  # 0..61 (kvotient vaerelse_id)
    number = models.PositiveSmallIntegerField(unique=True)  # 1..N (003, 101, … as int)
    floor = models.CharField(max_length=20)  # "stuen", "1. sal", …
    side = models.CharField(max_length=20)  # "mod gaden" / "mod gården"
    note = models.CharField(max_length=40, blank=True)  # "(røvhullet)", "(fængslet)", …


class Workgroup(models.Model):  # intern_alumne_workgroup
    legacy_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=100, unique=True)


class Cleaning(models.Model):  # intern_alumne_cleaning
    legacy_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=100, unique=True)
```

**ETL:** `Room` seeded by a fixture script that re-implements the `delt.php` loop (deterministic, no DB
source). `Workgroup`/`Cleaning` loaded from their legacy tables (utf8mb3) preserving `legacy_id`.

---

## 5. Batch 1 — Residents & Auth  (`residents` app)

### 5.1 Target models

```python
# residents/models.py
class ResidentManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)  # uses the configured hashers
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, **extra):
        extra.update(is_staff=True, is_superuser=True)
        return self.create_user(email, password, **extra)


class Resident(AbstractBaseUser, PermissionsMixin):
    # --- identity / login ---
    email = models.EmailField(
        unique=True
    )  # was intern_alumne.email (text) — now the login id
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=40, blank=True)
    # password: inherited from AbstractBaseUser; legacy unsalted sha256 imported via custom hasher

    # --- residency facts ---
    birthday = models.DateField(null=True, blank=True)
    move_in_date = models.DateField(null=True, blank=True)
    move_out_date = models.DateField(null=True, blank=True)
    study = models.CharField(
        max_length=255, blank=True
    )  # free text today (F-012 canonicalisation = open Q)

    # --- lineage (fylgje / sponsor) ---
    sponsor = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="proteges",
    )
    fylgje_raw = models.CharField(
        max_length=255, blank=True
    )  # original free-text, kept when unresolved

    # --- network access flag (alumneliste) ---
    is_network_closed = models.BooleanField(default=False)
    network_closed_details = models.TextField(blank=True)

    # --- Django auth ---
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(
        default=False
    )  # = member of any admin role group (set during ETL)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = ResidentManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]
```

**Roles → Groups.** Create seven `Group`s once: `editpage, indstilling, administrator, ak, inspektion,
kokkengruppe, oelkaelder`. For each `gahk_admin_user` row, add the linked Resident to every group whose
flag `!= 0`. App-side authorization (the per-controller checks in F-002…F-014) becomes group/permission
checks. `is_staff = True` if the resident is in any of these groups (so they can reach the admin area).

**Legacy password hasher** (`core/hashers.py`): a `BasePasswordHasher` with `algorithm = "gahk_sha256"`
that verifies `sha256(password) == legacy_hex`. ETL imports the password as
`gahk_sha256$$<legacy_hex>`. On a successful login Django re-hashes to the default (Argon2) — the
upgrade-on-login path (scope §5). No salt existed, so this is best-effort; document that these stay
weak until each user logs in.

### 5.2 Residency (per-month membership) — `intern_alumne_liste`

```python
# residents/models.py
class Residency(models.Model):
    resident = models.ForeignKey(
        Resident, on_delete=models.CASCADE, related_name="residencies"
    )
    room = models.ForeignKey(
        "core.Room", on_delete=models.PROTECT, related_name="residencies"
    )
    workgroup = models.ForeignKey(
        "core.Workgroup", null=True, blank=True, on_delete=models.SET_NULL
    )
    cleaning = models.ForeignKey(
        "core.Cleaning", null=True, blank=True, on_delete=models.SET_NULL
    )
    year = models.PositiveSmallIntegerField()  # decoded from monthNumber
    month = models.PositiveSmallIntegerField()  # 1..12, decoded from monthNumber

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["resident", "year", "month"], name="uniq_resident_month"
            )
        ]
        indexes = [models.Index(fields=["year", "month"])]
```

### 5.3 ETL mapping — Residents & Auth

| Legacy `intern_alumne` | → `Resident` | Transform |
|---|---|---|
| `ID` | `id` (PK preserved) | — |
| `firstName` / `lastName` | `first_name` / `last_name` | utf8mb3→UTF-8; trim |
| `email` | `email` | lower/trim; **dedupe = keep the row with the highest legacy `ID` per email; drop empty-email rows entirely** (decided 2026-06) |
| `password` | `password` | wrap as `gahk_sha256$$<hex>`; blanks → unusable password |
| `phone` | `phone` | trim |
| `birthday`,`moveInDay`,`moveOutDay` | `birthday`,`move_in_date`,`move_out_date` | `date`; `0000-00-00`/empty → NULL |
| `study` | `study` | trim |
| `fylgje` | `sponsor` (+`fylgje_raw`) | resolve by name match to a Resident; unresolved → `sponsor=NULL`, keep `fylgje_raw` + log (F-011) |
| `networkClosed` | `is_network_closed` | tinyint→bool |
| `networkClosedDetails` | `network_closed_details` | — |
| — | `is_staff` | True if in any role group |
| — | groups | from `gahk_admin_user` flags (one M2M per non-zero flag) |

| Legacy `intern_alumne_liste` | → `Residency` | Transform |
|---|---|---|
| `ID` | `id` (preserved) | — |
| `alumne_ID` | `resident_id` | FK; validate target exists |
| `room` | `room_id` | map room **number** → `Room` (by `number`) |
| `workgroup` (text) | `workgroup_id` | match to `Workgroup.name`; create-if-missing or NULL+log |
| `cleaning` (text) | `cleaning_id` | match to `Cleaning.name`; NULL+log if absent |
| `monthNumber` | `year`,`month` | decode `12*y+m` |

**Validation gates:** duplicate `email` → **keep the highest-`ID` row, drop the rest**; **empty `email`
→ drop the row** (those residents cannot log in; both actions logged); `alumne_ID` with no matching alumne (orphan residency);
`monthNumber` of 0/garbage; `fylgje` resolution rate (report % unmatched).

**Findings fixed by this design:** unsalted-sha256 exposure mitigated via upgrade-on-login (F-002/F-014);
scattered/inverted role checks → one group model (F-002/F-004/F-005/F-010 authz class); SQLi in the
auth/login queries gone via ORM (F-002/F-014); secrets out of source (the directory's `delt.php`
credentials disappear, F-010).

---

## 6. Batch 1 — Admissions  (`admissions` app)

### 6.1 Target model — `gahk_ansoegninger`

```python
# admissions/models.py
class Application(models.Model):
    class Type(models.TextChoices):
        TOUR = "rundvisning", "Rundvisning"
        SUBLET = "fremleje", "Fremleje"

    class Gender(models.TextChoices):
        MALE = "male", "Mand"
        FEMALE = "female", "Kvinde"
        OTHER = "other", "Andet"

    type = models.CharField(max_length=20, choices=Type.choices)
    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    age = models.CharField(max_length=50, blank=True)  # free-text today; keep faithful
    gender = models.CharField(
        max_length=10, choices=Gender.choices, blank=True
    )  # legacy stored only a `female` bool
    # tour-only
    study_year = models.CharField(max_length=255, blank=True)
    year_left = models.CharField(max_length=255, blank=True)
    university = models.CharField(max_length=255, blank=True)
    field_of_study = models.CharField(max_length=255, blank=True)
    # sublet-only
    occupation = models.CharField(max_length=255, blank=True)
    # shared
    heard_about_us = models.CharField(max_length=255, blank=True)
    motivation = models.TextField(blank=True)
    submitted_at = models.DateTimeField()  # from epoch `timestamp`
    received_by = models.ForeignKey(
        "residents.Resident",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applications_received",
    )
    received_at = models.DateTimeField(
        null=True, blank=True
    )  # NEW (legacy had no timestamp for this)

    class Meta:
        indexes = [
            models.Index(fields=["type", "submitted_at"]),
            models.Index(fields=["received_by"]),
        ]
```

### 6.2 ETL mapping — Admissions

| Legacy `gahk_ansoegninger` | → `Application` | Transform |
|---|---|---|
| `id` | `id` (preserved) | — |
| `typeOfAnsoegning` | `type` | map to choices; unknown → log |
| `fullName`,`email` | `full_name`,`email` | **latin1→UTF-8** (mojibake repair); trim |
| `age` | `age` | kept as text (legacy is free-text) |
| `female` | `gender` | `1`→`female`, `0`→`male` (legacy was binary; `other` reserved for new records) — assumption logged |
| `studyyear`,`yearleft`,`university`,`fieldofstudy`,`occupation`,`heardAboutUs`,`motivation` | same (snake_case) | latin1→UTF-8 |
| `day`,`month`,`year`,`timestamp` | `submitted_at` | build from epoch `timestamp` (Europe/Copenhagen); cross-check vs `day/month/year`, log mismatches |
| `receivedByAlumneId` | `received_by_id` | `0`/NULL → NULL; else FK to Resident (validate) |
| — | `received_at` | NULL (no legacy source) |

**Findings fixed:** mass-assignment (insert-whole-`$_POST`) → explicit form/fields (F-001); SQLi in the
list/detail queries → ORM (F-001). `gender` is mapped from the legacy binary (`female`→`female`, else
`male`; `other` reserved for new records — decided 2026-06); the `fremleje` committee-email `if(TRUE)`
behaviour is preserved as data faithfully and flagged for the fix-vs-preserve decision (F-001).

---

## Batch 2 — CMS: pages, news, pylon calendar  (`cms` app)

Three independent content tables (no FK to residents). The shared sins here are **stored XSS**
(HTML rendered raw — F-006/07/08), **mass-assignment** (`insert($_POST)`), epoch/`day,month,year` date
columns, and the **slug↔page-id coupling living in `routes.php`** (F-006). Clean design fixes all four.

### Target models

```python
# cms/models.py
class Page(models.Model):  # gahk_page
    menu_category = models.PositiveSmallIntegerField(
        default=0
    )  # was menuCat (nav highlighting)
    slug = models.SlugField(
        max_length=80, unique=True, blank=True
    )  # NEW — replaces routes.php mapping
    header = models.CharField(max_length=255)
    body = models.TextField(
        blank=True
    )  # was `text` (mediumtext HTML); sanitized on save (see below)
    background_image = models.CharField(max_length=255, blank=True)  # was bgpic


class NewsItem(models.Model):  # gahk_news
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)  # was `text`; sanitized
    published_at = models.DateTimeField()  # from epoch `timestamp`


class PylonEvent(models.Model):  # gahk_pylon_calendar
    title = models.CharField(max_length=255)  # was `name`
    description = models.TextField(blank=True)  # sanitized
    starts_on = models.DateField()  # from day/month/year (the displayed date)


class Event(models.Model):  # NEW — replaces the hard-coded array in begivenheder.php
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    starts_on = models.DateField()
```

**HTML sanitization (the F-006/07/08 stored-XSS fix).** `Page.body`, `NewsItem.body`,
`PylonEvent.description` hold editor (CKEditor) HTML. Sanitize with an allowlist (e.g. **nh3/bleach**)
**on save** (model `clean()`/`save()`), and render with autoescape-off only on the already-sanitized
field. The ETL **also sanitizes once on import** so any payload already sitting in the data is cleaned.
`background_image` is rendered into CSS `url(...)` → validate/escape it (no `)`/`;`/quotes).

**Slugs (the F-006 fix).** Legacy has no slug; URLs are mapped in `routes.php`
(`velkommen→page/show/1`, `faciliteter→2`, `kollegielivet→3`, `vision→22`, `legater→4`, `kontakt→21`,
`faciliteter/vaerelse→10`, …). Seed `Page.slug` from that mapping (a fixture), so the slug↔page
relationship lives in the DB and the public URLs are **preserved verbatim** for 301s (scope §6). Pages
with no named slug remain reachable by id/auto-slug.

### ETL mapping

| Legacy `gahk_page` (utf8mb3) | → `Page` | Transform |
|---|---|---|
| `id` | `id` (preserved) | — |
| `menuCat` | `menu_category` | — |
| `header` | `header` | trim |
| `text` | `body` | **sanitize HTML** (allowlist) |
| `bgpic` | `background_image` | trim; validate for CSS-url safety |
| — | `slug` | seed from `routes.php` named-route map |

| Legacy `gahk_news` (latin1) | → `NewsItem` | Transform |
|---|---|---|
| `id` | `id` (preserved) | — |
| `title` | `title` | **latin1→UTF-8** |
| `text` | `body` | latin1→UTF-8; **sanitize HTML** |
| `day,month,year,timestamp` | `published_at` | from epoch `timestamp` (Europe/Copenhagen); cross-check vs y/m/d |

| Legacy `gahk_pylon_calendar` (utf8mb3) | → `PylonEvent` | Transform |
|---|---|---|
| `id` | `id` (preserved) | ⚠ legacy `id` has no AUTO_INCREMENT in the dump (F-008 open Q) — clean PK is a proper sequence; preserve values + bump sequence |
| `name` | `title` | trim |
| `description` | `description` | **sanitize HTML** |
| `day,month,year` (+`timestamp`) | `starts_on` | build `date` from y/m/d (the `timestamp` is redundant) |

`Event`: **no legacy table** — seed from the hard-coded array in `application/views/news/begivenheder.php`
(a one-off fixture script), then it becomes editable content.

**Findings fixed:** stored XSS in CMS/news/pylon content (F-006/07/08) via sanitize-on-save + sanitized
import + template autoescaping; mass-assignment → explicit fields; the Sep/Okt month-label swap (F-001/F-008)
disappears because dates are stored as real `date`s, not rendered from a buggy month array; pylon PK
integrity (F-008); slug↔id coupling → DB-driven slugs (F-006). The **unauthenticated `news/delete`**
(F-007) and missing CSRF become a normal permission-guarded, CSRF-protected Django delete view.

**Open (carried):** is the news feature live or retired (F-007)? If retired, `NewsItem` is imported as
**archive-only** (read-only history) and the create/edit UI is dropped — the rest of the batch is unaffected.

---

## 7. Remaining domain batches (to do next, same format)

| Batch | App / tables | Notable transforms / fixes |
|---|---|---|
| 2 | `cms`: `gahk_page`, `gahk_news`, `gahk_pylon_calendar` | ✅ **Done — see the "Batch 2 — CMS" section above.** |
| 3 | `ak`: `intern_alumne_aklog`, `intern_alumne_akstatus` | derive status from log instead of the buggy `monthNumber='24178'` / `-1*` writes (F-009 — needs intent sign-off); decimal/int krydser |
| 4 | `rooms` (kvotient): `intern_kvotient_nyintern`+`_priority`+`_orlov`+`_offer` | atomic multi-table submit (F-004); FK to Room; store `K` as computed/derived not a string; drop cross-month cascade-delete bug |
| 5 | `rooms` (condition): `intern_room_condition`, `intern_room_criteria` | **normalize** the `:`/`;`/`\|` delimited criteria/comment/image blobs into rows (F-005); proper datetime; FileField uploads; `is_newest` → query/flag with integrity |
| 6 | `oelkaelder`: `product,saldo,deposit,transaction,transaction_item,purchase,log,warnings,intern_shopper` | money as **integer øre**; **atomic** purchase/deposit (F-003); server-side pricing; balance as derived ledger vs stored `saldo` (decision) |
| 7 | `residents` remainder + `stats` | `StudyProgramme` lookup; email-settings tables (`intern_alumne_emailtonew/_emailnetworkstatus/_emailsubscribers/_pylon_email_settings`); optional `DailyVisitCount` |

---

## 8. ETL mechanics

1. **Extract/stage:** `mysqldump` the live `gahk_dk`; load into a throwaway MariaDB (so charset metadata
   is honoured), then **pgloader** lifts the App-relevant tables 1:1 into a Postgres `staging` schema
   (fast bulk copy, handles latin1→utf8 at this step).
2. **Transform/load:** Python management commands (`manage.py etl_<domain>`) read `staging.*` and write the
   clean models in dependency order (§3), preserving PKs and applying §5/§6 maps. Each command is an
   **upsert** keyed on the preserved PK → re-runnable.
3. **Resolve cross-refs second pass:** `fylgje → sponsor` and any name→lookup matches run after all
   Residents exist (so self-FKs resolve), emitting an unresolved-report.
4. **Validate & diff:** after load, assert row counts vs staging, FK integrity, and run the spec-driven
   "diff against old site" sampler. Reset sequences: `SELECT setval(...) = max(id)+1` per table.
5. **Encoding QA:** a targeted check for mojibake (`Ã¦ Ã¸ Ã¥` etc.) on the latin1-origin tables after load.

---

## 9. Decisions & remaining open questions

**Decided (2026-06):**
- Schema form = **Django models** (migrations generate the DDL).
- **Email dedup:** keep the highest-`ID` row per email; **drop** empty-email rows.
- **Counter:** migrate `gahk_counterdato` → `stats.DailyVisitCount`; do **not** migrate `gahk_counter`. GDPR is not a concern for this project.
- **`gender`:** model `male`/`female`/`other`; ETL maps legacy `female` 1→`female`, 0→`male`; `other` only for new records.
- **`fylgje`:** confirmed = sponsor → `Resident.sponsor` self-FK.

**Still open (do not block Batches 1–2):**
- **`study` canonicalisation** — keep free-text or introduce a `StudyProgramme` lookup (F-012)? Affects Batch 7.
- **News vs Facebook** — rebuild the news feature or retire it (F-007)? Determines whether `cms.NewsItem`
  ships live or archive-only.
- The **fix-vs-preserve business-logic calls** in `99-index.md` §4-C (fremleje email, kvotient K-formula &
  cross-month cascade delete, AK month/negative-insert) — shape Batches 3/4/6; need committee sign-off.

---

## 10. Findings confirmed while running the ETL/build (2026-06/07)

- **Sequences must be reset after the preserved-PK load.** `bulk_create`/explicit-id inserts do NOT advance
  Postgres sequences, so the first ORM `create()` collides at `id=1`. Added `manage.py reset_sequences`
  (resets all 8 apps' models) as the **final step of the ETL** (`task etl`).
- **Duplicate emails: 5** (121 residents → 116 distinct). Merged into the highest-`ID` row; their
  residencies/roles re-point to the kept resident (`resident_id_remap`).
- **Residency orphans (accepted, option A).** `intern_alumne_liste` references **434 distinct people across
  193 months**, but `intern_alumne` retains only 121; ~313 former residents survive only as an ID with **no
  recoverable personal data**. Their historical residencies (8 858 rows) are **not migrated** — nothing to
  attach them to. Same root cause caps F-011 (stamtree) "moved-out" completeness.
- **Kvotient data is corrupt at source.** `intern_kvotient_offer_nyintern` is **empty (0 rows)** and **all
  1 393 priorities are orphaned** (reference application ids that don't exist — fallout from the legacy
  `closeOffer` cross-month cascade-delete bug). Migrated: 15 applications, **0 priorities, 0 offers**. F-004
  is therefore a **fresh-start** feature (apply form + offer admin), not a data port.
- **Room-condition dates** are already proper `datetime` in the dump (not the feared `YmdHis` strings) — trivial ETL.
- **Ølkælder log** is large (**104 525 rows**) — the ETL bulk-creates it. Money verified: shares sum exactly
  to item totals across 37 400 transactions (largest-remainder split).
- **Media relocation is best-effort.** `manage.py relocate_media` copies referenced legacy image files into
  `MEDIA_ROOT`. Most **product** `imageurl`s were absolute URLs (skipped, ~200) and many room-image paths
  don't resolve to files in the dump (~254 missing); the ones present are copied. New inspection uploads go
  to `MEDIA_ROOT/roomimages/`.
- **Visit counter:** `gahk_counter` (per-IP) is **not** migrated; a front-page-only middleware
  (`stats.middleware`) records visits with **HMAC-hashed** IPs going forward, and `gahk_counterdato`
  history is migrated to `stats.DailyVisitCount`.
