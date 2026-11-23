# Instructions for coding agents

## Respect the domain boundaries

Each Django app under `app/` owns its data and its rules. The full rules are in
[spec/architecture.md](spec/architecture.md). Follow them in all new and changed code:

1. **Import another app only through its `api.py`.** Never through its `models`, `services`,
   `views`, `forms`, `permissions` or private helpers. That includes `residents` and
   `notifications`.
2. **Imports point down the layers:** composition → features → platform (`notifications`) →
   identity (`residents`) → kernel (`core`). `core` never imports an app above it. A feature never
   imports another feature unless `tach.toml` declares that link.
3. **Write only your own app's tables.** To change another app's data, call a command in its
   `api.py`. If none exists, add one there, in the same PR. Do not `.save()`, `.update()` or
   `.delete()` an instance or queryset of another app's model, and do not write through a reverse
   relation.
4. **New relations into another app set `related_name="+"`.**
5. **Store files only under your app's own prefixes** (`storage_prefixes` in its `apps.py`). A new
   kind of file gets a prefix of its own, declared there, in English.
6. **No raw SQL** (`django.db.connection`, `connections`, `RawSQL`, `.raw()`) outside the kernel
   files allow-listed in `app/ruff-architecture.toml`.
7. **Showing another app's content on your page:** ask its `api` for plain data. If a lower layer
   needs a feature's content, the lower layer offers a `register_…` extension point, and the
   feature registers with it from its `AppConfig.ready()`.

## Check your change

- `task architecture` runs Tach, the raw-SQL rule and `manage.py check_architecture`.
- Never run `tach sync` or `tach mod`, whatever Tach's output suggests: they rewrite `tach.toml`
  to allow whatever the code does today, which defeats the check.
- The test suite reports runtime crossings at the end of each run, under "architecture".
  `ARCHITECTURE_STRICT=1` turns them into test failures.
- CI's **Architecture** workflow compares the PR with its base. Its summary lists what the PR adds.

The checks are not required to pass yet: dev has many old findings. **Do not add new ones.** If
you touch code with an old finding and the fix is small, fix it. When a rule truly does not fit,
say so in the PR description instead of working around it.
