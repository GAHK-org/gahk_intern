# ny_ny_intern
[![CI](https://github.com/GAHK-org/gahk_intern/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/GAHK-org/gahk_intern/actions/workflows/ci.yml)


## Kom i gang (udvikling)

```sh
task install     # opret virtualenv + installer afhængigheder
task seed        # starter Postgres+MinIO, fylder databasen med realistisk falsk demo-data
task dev         # hele appen i Docker (Postgres+MinIO+Django, hot-reload) → http://127.0.0.1:8800
```

`task dev` kører Django i en container (`task dev:down` for at stoppe, `task dev:logs` for logs).
Foretrækker du at køre Django direkte på systemet i stedet: `task dev:local` — samme
Postgres+MinIO-containere, bare uden web-containeren. Begge starter automatisk Postgres + MinIO i
Docker (`task services:up`) og peger appen på dem via `app/.env` (kopiér `app/.env.example`). MinIO
erstatter lokalt Hetzner Object Storage til uploads; `task minio:console` viser login til dets
webgrænseflade. `task db:up` starter derudover MariaDB, som kun bruges til ETL fra det gamle site.

`task seed` genererer deterministisk demo-data (beboere, værelser, AK, ølkælder,
ansøgninger, opslagstavlen m.m.), så nye udviklere ser en udfyldt side med det samme. Kommandoen
kan køres igen når som helst (`--fresh` rydder først). Logins (kodeord `demo1234`):

| Email | Adgang |
|-------|--------|
| `admin@gahk.dk` | superbruger (alt) |
| `formand@gahk.dk` | administrator-rolle |
| `ak@gahk.dk` | AK-rolle |
| `oel@gahk.dk` | Ølkælder-rolle |
| `beboer@gahk.dk` | almindelig beboer (kan bruge opslagstavlen, men ikke Den Hurtige endnu) |

## Test

```sh
task test:pg      # kør testsuiten mod Postgres (som CI); kræver `task db:up`
task test:sqlite  # kør testsuiten mod SQLite (uden Docker)
```

## Code Quality: lint + typer + pre-commit

```sh
task lint         # ruff check + format-tjek
task typecheck    # mypy med django-stubs (samme som CI's typecheck-job)
task hooks        # installér prek git pre-commit hooks (kør én gang)
```

`task hooks` installerer [prek](https://github.com/j178/prek) (en hurtig, pre-commit-kompatibel
runner) via `.pre-commit-config.yaml`. Derefter kører **ruff** (check + format) og **mypy** automatisk
ved hvert commit. Kør manuelt på alt med `uv run prek run --all-files`. De samme tjek håndhæves i CI
(`.github/workflows/ci.yml`): `lint`, `typecheck`, `test`, `security` og `build`.
