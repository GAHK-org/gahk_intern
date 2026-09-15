# gahk_intern
[![CI](https://github.com/GAHK-org/gahk_intern/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/GAHK-org/gahk_intern/actions/workflows/ci.yml)


## Kom i gang (udvikling)

```sh
task install     # opret virtualenv + installer afhængigheder
task db:up       # start Postgres + MariaDB i Docker
task seed        # fyld databasen med realistisk falsk demo-data
task dev         # kør udviklingsserveren
```

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
