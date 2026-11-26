"""Who owns what: the lookups behind the architecture checks (spec/architecture.md).

Every project app owns its database tables and the storage names under the prefixes its AppConfig
declares in `storage_prefixes`. Only the owner writes them; other apps go through the owner's `api`.

Two consumers, neither of them production code:
  * `manage.py check_architecture` (static): relations that hand another app a reverse accessor,
    and FileFields that store outside their app's prefixes.
  * tests/architecture_guard.py (runtime, under pytest): writes to another app's tables or prefixes,
    and SQL that did not come from the ORM.

Tach (tach.toml) checks the imports; this module covers what an import check cannot see.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from django.apps import AppConfig, apps
from django.conf import settings
from django.db import models

# The kernel: core and the audit trail (tach.toml). Their helpers act on behalf of whoever calls
# them, so a write that passes through the kernel is attributed to the app that called it
# (tests/architecture_guard.py). The audit trail's own rows are still the audit app's.
KERNEL = frozenset({"core", "audit"})


@dataclass(frozen=True, order=True)
class Finding:
    """One violation, keyed without line numbers so a report survives unrelated edits."""

    check: str  # "relation" | "storage-prefix" | "storage-field"
    app: str  # the app whose code or model breaks the rule
    subject: str  # e.g. "events.Rsvp.resident"
    message: str


@cache
def project_apps() -> dict[str, AppConfig]:
    """This project's own apps by module name: everything whose code lives under BASE_DIR."""
    base = Path(settings.BASE_DIR).resolve()
    return {
        config.name: config
        for config in apps.get_app_configs()
        if Path(config.path).resolve().is_relative_to(base)
    }


def app_of_module(module: str) -> str | None:
    """The project app a dotted module path belongs to, or None (Django, third-party, tests)."""
    top = module.partition(".")[0]
    return top if top in project_apps() else None


def app_of_model(model: type[models.Model]) -> str | None:
    return app_of_module(model.__module__)


@cache
def table_owners() -> dict[str, str]:
    """db_table -> owning app, including the auto-created through tables of ManyToManyFields
    (owned by the app that declares the field)."""
    owners: dict[str, str] = {}
    for model in apps.get_models(include_auto_created=True):
        declaring = model._meta.auto_created  # the model that declares an auto-created through table
        owner = app_of_model(declaring if isinstance(declaring, type) else model)
        if owner:
            owners[model._meta.db_table] = owner
    return owners


@cache
def storage_prefixes() -> dict[str, str]:
    """Declared storage-name prefix -> owning app, from each AppConfig's `storage_prefixes`."""
    prefixes: dict[str, str] = {}
    for name, config in project_apps().items():
        for prefix in getattr(config, "storage_prefixes", ()):
            prefixes[prefix] = name
    return prefixes


def owner_of_storage_name(name: str) -> str | None:
    """The app whose declared prefix holds `name` (longest prefix wins), or None."""
    name = name.lstrip("/")
    matches = [prefix for prefix in storage_prefixes() if name.startswith(prefix)]
    return storage_prefixes()[max(matches, key=len)] if matches else None


def relation_findings() -> list[Finding]:
    """Relations into another app that give that app a reverse accessor onto this app's table.

    `event.notices.all()` needs no import at all, so Tach cannot see it: a ForeignKey("events.Event")
    declared in opslagstavle hands every Event a manager onto opslagstavle's table. The rule is
    `related_name="+"` on every relation into another app. The declaring side keeps its forward
    accessor; the other app asks the declaring app's `api` instead.
    """
    findings = []
    for model in apps.get_models():
        source = app_of_model(model)
        if source is None:
            continue
        # The relations this model declares itself: FKs, one-to-ones and many-to-manys.
        for field in [*model._meta.local_fields, *model._meta.local_many_to_many]:
            if not field.is_relation:
                continue
            target_model = field.related_model
            if not isinstance(target_model, type):
                continue
            target = app_of_model(target_model)
            if target is None or target == source:
                continue
            remote = field.remote_field
            if remote is not None and remote.hidden:
                continue
            subject = f"{model._meta.app_label}.{model.__name__}.{field.name}"
            findings.append(
                Finding(
                    "relation",
                    source,
                    subject,
                    f"{subject} gives {target}.{target_model.__name__} a reverse accessor onto a {source} table; "
                    'set related_name="+"',
                )
            )
    return findings


def _prefix_overlaps() -> list[Finding]:
    findings = []
    owned = sorted(storage_prefixes().items())
    for prefix, owner in owned:
        if not prefix.endswith("/"):
            findings.append(
                Finding(
                    "storage-prefix",
                    owner,
                    prefix,
                    f"storage prefix {prefix!r} of {owner} must end in '/' (a whole folder)",
                )
            )
        for other, other_owner in owned:
            if other != prefix and other.startswith(prefix) and other_owner != owner:
                findings.append(
                    Finding(
                        "storage-prefix",
                        other_owner,
                        other,
                        f"storage prefix {other!r} of {other_owner} lies inside {prefix!r} of {owner}",
                    )
                )
    return findings


def storage_findings() -> list[Finding]:
    """Declared prefixes must not overlap, and each FileField must store under its own app's.

    A string `upload_to` is checked against the prefixes directly. A callable one, and a callable
    `storage`, must at least be the app's own code (or the kernel's); the names they produce are
    checked at runtime by tests/architecture_guard.py.
    """
    findings = _prefix_overlaps()
    for model in apps.get_models():
        owner = app_of_model(model)
        if owner is None:
            continue
        for field in model._meta.get_fields():
            if not isinstance(field, models.FileField):
                continue
            subject = f"{model._meta.app_label}.{model.__name__}.{field.name}"
            upload_to = field.upload_to
            if callable(upload_to):
                code_owner = app_of_module(getattr(upload_to, "__module__", ""))
                if code_owner != owner and code_owner not in KERNEL:
                    findings.append(
                        Finding(
                            "storage-field",
                            owner,
                            subject,
                            f"{subject} names its files with {code_owner}'s code; use {owner}'s own",
                        )
                    )
            elif upload_to and owner_of_storage_name(str(upload_to)) != owner:  # "": code names each file
                findings.append(
                    Finding(
                        "storage-field",
                        owner,
                        subject,
                        f"{subject} stores under {upload_to!r}, outside {owner}'s storage_prefixes",
                    )
                )
            storage_callable = getattr(field, "_storage_callable", None)
            if storage_callable is not None:
                code_owner = app_of_module(getattr(storage_callable, "__module__", ""))
                if code_owner not in (owner, None) and code_owner not in KERNEL:
                    findings.append(
                        Finding(
                            "storage-field",
                            owner,
                            subject,
                            f"{subject} stores through {code_owner}'s storage; use {owner}'s own",
                        )
                    )
    return findings
