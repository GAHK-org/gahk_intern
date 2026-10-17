# gahk_intern
[![CI](https://github.com/GAHK-org/gahk_intern/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/GAHK-org/gahk_intern/actions/workflows/ci.yml)


## Kom i gang (udvikling)

```sh
task install     # opret virtualenv + installer afhængigheder
task seed        # starter Postgres+S3, fylder databasen med realistisk falsk demo-data
task dev         # hele appen i Docker (Postgres+S3+Django, hot-reload) → http://127.0.0.1:8800
```

`task dev` kører Django i en container (`task dev:down` for at stoppe, `task dev:logs` for logs).
Foretrækker du at køre Django direkte på systemet i stedet: `task dev:local` — samme
Postgres+S3-containere, bare uden web-containeren. Begge starter automatisk Postgres + S3 i
Docker og peger som standard appen på dem; brug `app/.env` (kopiér `app/.env.example`) for at
tilpasse databasen eller andre indstillinger. SeaweedFS
erstatter lokalt Hetzner Object Storage til uploads; `task s3:console` viser adressen på dets
filbrowser.

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

## Domænegrænser

Hver Django-app ejer sine egne data og regler. Andre apps må kun bruge dens `api.py` (kommandoer,
forespørgsler og events med id'er og DTO'er), aldrig dens modeller eller services.

```sh
task boundaries        # tach check + ORM-relationer på tværs af domæner + rå SQL
task boundaries:graph  # tegn afhængighedsgrafen (Mermaid)
```

Reglerne, hvad der håndhæves automatisk, og hvad der kun fanges i review, står i
[docs/domain-boundaries.md](docs/domain-boundaries.md). CI tjekker dem på hver PR
(`.github/workflows/boundaries.yml`).

## Branches og pull requests

Ingen kan pushe direkte til `main` eller `dev`; alt går gennem pull requests. `dev` samler det
næste release, og `main` er det, der kører i produktion.

| Branch | Laves fra | PR ind i | Merge med |
|--------|-----------|----------|-----------|
| `feature/…`, `fix/…` (og Copilots `copilot/…`) | `dev` | `dev` | **Squash and merge** |
| `dev` (release) | – | `main` | **Create a merge commit** |
| `hotfix/…` | `main` | `main` | **Squash and merge** |
| `automation/sync-main-…` (laves automatisk) | – | `dev` | **Create a merge commit** – aldrig squash |
| Dependabot | – | `dev`; sikkerhedsopdateringer: `main` | **Squash and merge** (patch-opdateringer merges automatisk) |

**Release:** Åbn en PR fra `dev` til `main`, og merge med "Create a merge commit". Squash eller rebase
aldrig en release: så deler `main` og `dev` ikke længere historik, og hver release giver de samme
konflikter igen.

**Hotfix** – en rettelse, der ikke kan vente på næste release:

1. Lav branchen fra `main`, ikke `dev`: `git fetch && git switch -c hotfix/kort-navn origin/main`.
2. Åbn en PR ind i `main`, og squash-merge den.
3. Workflowet *Sync main to dev* laver en branch med `main` merget ind i `dev`. Åbn PR'en via linket i
   kørslens opsummering (Actions-fanen), og merge med "Create a merge commit".

Indtil sync-PR'en er merget, er PR'er ind i `dev` blokeret – så rettelsen ikke går tabt i næste
release. Det samme gælder, når Dependabot har merget en sikkerhedsopdatering ind i `main`.

**Tjekkene `branch-flow` og `in-sync`** (`.github/workflows/branch-policy.yml`) håndhæver reglerne:

- `branch-flow` fejler: branchen må ikke merges derind (tjek navnet og målet), eller en hotfix
  indeholder upubliceret arbejde fra `dev` – lav den forfra fra `main`.
- `in-sync`: *"main has changes dev lacks"* – merge sync-PR'en ind i `dev` først.
- `in-sync`: *"main has moved on"* – klik "Update branch" (på Dependabots PR'er: kommentér
  `@dependabot rebase`).
