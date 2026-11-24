# Architecture: domain boundaries

GAHK Intern is a modular monolith: one Django project, one database, one bucket, and inside it
apps that each own their data and their rules. Other apps reach an app only through its public
`api.py`. The checks below report every crossing. They are not required to pass yet; the totals
start high and fall as the code is moved over. New code must not add to them.

## Layers

Imports point down this list, never up.

| Layer | Modules | May import |
|---|---|---|
| composition | `config`, `core.context_processors`, `core.middleware`, `core.management`, `core.demo`, `residents.views_admin`, `residents.urls_admin`, `audit.views`, `audit.demo` | anything, including internals. Wiring and operator tooling only, never business rules |
| features | `admissions`, `ak`, `cms`, `den_hurtige`, `events`, `gahkflix`, `oelkaelder`, `opslagstavle`, `photo_album`, `regnskab`, `reparationer`, `rooms`, `stats` | the `api` of the layers below. A sibling feature's `api` only if `tach.toml` lists it in `depends_on`. None are listed yet |
| platform | `notifications`; `core.admin_summary`, `core.live`, `core.mentions`, `core.push`, `core.rollout`, `core.tasks` | identity's `api`, kernel |
| identity | `residents` | kernel |
| kernel | the rest of `core`; `audit` (the audit trail) | nothing above it |

## The rules

- **Imports:** from another app, import only its `api` module. That holds for `residents` and
  `notifications` too; their models and permissions are private. The one exception is `audit`,
  whose public module is `audit.trail`: every app records its events through it.
- **Tables:** only the owning app inserts, updates or deletes rows in its tables. Everyone else
  calls a command in the owner's `api`. A cascade (`on_delete`) counts as the declaring app's choice.
- **Relations:** a ForeignKey, OneToOne or ManyToMany into another app sets `related_name="+"`, so
  the other app gets no reverse accessor onto this app's table.
- **Storage:** each app declares the storage-name prefixes it owns (`storage_prefixes` on its
  AppConfig, ending in `/`). Prefixes do not overlap, and only the owner saves or deletes under them.
- **Raw SQL:** none outside the kernel's infrastructure (health check, Celery's and kombu's tables,
  locks, sequences, the audit trail's retention purge). The allow-list is the per-file-ignores in
  `app/ruff-architecture.toml`.

## What goes in `api.py`

- **Queries:** functions that return plain data (frozen dataclasses, ids, primitives) or a queryset
  of ids, never a model instance someone could `.save()`.
- **Commands:** functions that change this app's data and enforce its rules.
- **Extension points:** `register_…` functions through which other apps add content, so a lower
  layer can show a feature's content without importing it.

## The checks

| Check | Catches | Run by |
|---|---|---|
| Tach (`tach.toml`) | imports of another app's internals, undeclared links between features, imports up a layer, cycles; imports inside functions too | `uv run tach check` |
| `manage.py check_architecture` | relations without `related_name="+"`, overlapping storage prefixes, FileFields outside their app's prefixes | the command |
| ruff (`app/ruff-architecture.toml`) | importing `django.db.connection(s)` or `RawSQL` outside the allow-list | `ruff check --config ruff-architecture.toml` |
| Test-suite guard (`app/tests/architecture_guard.py`) | writes to another app's tables or prefixes, whatever the route (`.save()` on a handed instance, reverse relations, `get_model`, `.raw()`); SQL not from the ORM outside the allow-list | every pytest run |

- `task architecture` runs the first three. The tests print the guard's findings at the end of each
  run. `ARCHITECTURE_STRICT=1` makes a test fail when it crosses a boundary.
- CI's Architecture workflow runs all four on the pull request and on its base. Its summary lists
  what the PR adds and what it fixes.

## Not checked, so left to review

- Following a forward relation into another app (`notice.event.title`) and `__` lookups across apps
  (`filter(event__visibility=…)`). Read the id, and ask the other app's `api`.
- Reads of another app's tables by raw SQL from allow-listed code, and anything in threads the
  tests do not run. The guard only sees the code paths the tests exercise.
- URL names, template variables and Celery task names: strings, not imports.

## When the rules are in the way

- **You need data another app's `api` does not offer:** add a query to that app's `api.py` in the
  same PR. That is the intended route.
- **A feature truly depends on a sibling:** add it to that module's `depends_on` in `tach.toml`
  with a comment saying why. If that makes a cycle, invert one direction with an extension point.
- **A one-off:** `# tach-ignore(reason) name` on the import line. A reason is required, and an
  ignore that no longer suppresses anything fails the check.

## Decisions

- **Strict from the start, nothing allow-listed.** `tach.toml` describes the target design, so
  today's code reports every gap. The list of agreed exceptions comes when the check becomes
  required.
- **Reported, not enforced, for now.** No Django system check (it would stop `runserver`), and the
  raw-SQL rule is kept out of `ruff.toml`, so the required lint, test and migration jobs are
  unaffected.
- **The guard runs only under pytest.** It reads the call stack on every write. Production never
  loads it, and tests cover the paths that matter.
- **No database role or schema per app.** One Django process uses one connection for every app, so
  every app would run as the same role anyway, and it would cost the cross-app foreign keys. The
  same goes for storage credentials per app.
- **Known debt:** `Room`, `Workgroup` and `Cleaning` are domain models living in `core.models`.
