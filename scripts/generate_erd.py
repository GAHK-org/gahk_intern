#!/usr/bin/env python
"""Generate a Mermaid erDiagram from Django models and write it to docs/erd.md.

Exit codes: 0 = no change, 1 = diagram was updated (stage docs/erd.md and recommit).
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
OUTPUT = REPO_ROOT / "docs" / "erd.md"

sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.apps import apps  # noqa: E402
from django.db import models as m  # noqa: E402


def is_project_app(app_config: object) -> bool:
    """True for apps whose code lives inside APP_DIR (not Django internals or third-party)."""
    return Path(app_config.path).is_relative_to(APP_DIR)


FIELD_TYPES: dict[str, str] = {
    "AutoField": "int",
    "BigAutoField": "int",
    "SmallAutoField": "int",
    "IntegerField": "int",
    "PositiveIntegerField": "int",
    "PositiveSmallIntegerField": "int",
    "SmallIntegerField": "int",
    "BigIntegerField": "int",
    "FloatField": "float",
    "DecimalField": "decimal",
    "BooleanField": "bool",
    "CharField": "string",
    "TextField": "text",
    "EmailField": "string",
    "SlugField": "string",
    "URLField": "string",
    "UUIDField": "string",
    "FileField": "string",
    "ImageField": "string",
    "DateField": "date",
    "DateTimeField": "datetime",
    "TimeField": "time",
    "JSONField": "json",
    "ForeignKey": "int",
    "OneToOneField": "int",
}


def entity_name(model: type) -> str:
    return f"{model._meta.app_label}_{model.__name__}"


def mermaid_type(field: m.Field) -> str:
    return FIELD_TYPES.get(field.get_internal_type(), "string")


def entity_block(model: type, stub: bool = False) -> list[str]:
    """Return the erDiagram entity lines for a model. Stubs have an empty body."""
    name = entity_name(model)
    if stub:
        return [f"    {name} {{ }}", ""]
    lines = [f"    {name} {{"]
    for field in model._meta.get_fields():
        if not field.concrete:
            continue
        if isinstance(field, m.ManyToManyField):
            continue
        if isinstance(field, (m.ForeignKey, m.OneToOneField)):
            lines.append(f"        int {field.attname} FK")
        else:
            suffix = " PK" if field.primary_key else ""
            lines.append(f"        {mermaid_type(field)} {field.name}{suffix}")
    lines.append("    }")
    lines.append("")
    return lines


def relation_lines(model: type, model_set: set[type]) -> list[str]:
    """Return erDiagram relationship lines for all FK/O2O/M2M fields on a model."""
    name = entity_name(model)
    lines = []
    for field in model._meta.get_fields():
        if not field.concrete:
            continue
        if isinstance(field, m.ManyToManyField):
            related = field.related_model
            if related in model_set:
                lines.append(
                    f'    {name} }}o--o{{ {entity_name(related)} : "{field.name}"'
                )
        elif isinstance(field, (m.ForeignKey, m.OneToOneField)):
            related = field.related_model
            if related in model_set:
                right = "|o" if field.null else "||"
                left = "||" if isinstance(field, m.OneToOneField) else "}o"
                lines.append(
                    f'    {name} {left}--{right} {entity_name(related)} : "{field.name}"'
                )
    return lines


def app_diagram(app_label: str, all_models: list[type]) -> str:
    """Build an erDiagram block for one app.

    Owned models get full field listings; models from other apps that are
    referenced by FK/M2M appear as empty stubs so the arrows have targets.
    """
    own = [mo for mo in all_models if mo._meta.app_label == app_label]
    model_set = set(all_models)

    # Collect cross-app models referenced by this app's FK/M2M fields
    stubs: set[type] = set()
    for model in own:
        for field in model._meta.get_fields():
            if not field.concrete:
                continue
            if isinstance(field, (m.ForeignKey, m.OneToOneField, m.ManyToManyField)):
                related = field.related_model
                if related in model_set and related._meta.app_label != app_label:
                    stubs.add(related)

    body: list[str] = []
    for model in own:
        body += entity_block(model)
    for model in sorted(stubs, key=lambda mo: entity_name(mo)):
        body += entity_block(model, stub=True)
    for model in own:
        body += relation_lines(model, model_set)

    inner = "\n".join(body).rstrip()
    return f"```mermaid\nerDiagram\n{inner}\n```"


def full_diagram(all_models: list[type]) -> str:
    model_set = set(all_models)
    body: list[str] = []
    for model in all_models:
        body += entity_block(model)
    for model in all_models:
        body += relation_lines(model, model_set)
    inner = "\n".join(body).rstrip()
    return f"```mermaid\nerDiagram\n{inner}\n```"


def generate(all_models: list[type]) -> str:
    app_labels = sorted({mo._meta.app_label for mo in all_models})
    sections: list[str] = ["## Full diagram\n\n" + full_diagram(all_models)]
    for app_label in app_labels:
        sections.append(f"## {app_label}\n\n{app_diagram(app_label, all_models)}")
    return "\n\n".join(sections)


def main() -> None:
    OUTPUT.parent.mkdir(exist_ok=True)

    all_models = [
        model
        for app in apps.get_app_configs()
        if is_project_app(app)
        for model in app.get_models()
    ]

    header = (
        "# Entity Relationship Diagram\n\n"
        "> Auto-generated — do not edit by hand. "
        "Re-run `uv run python scripts/generate_erd.py` to refresh.\n"
        "> Cross-app references appear as empty stub entities.\n\n"
    )
    content = header + generate(all_models) + "\n"

    if OUTPUT.exists() and OUTPUT.read_text(encoding="utf-8") == content:
        sys.exit(0)

    OUTPUT.write_text(content, encoding="utf-8")
    print(f"ERD updated: {OUTPUT.relative_to(REPO_ROOT)}")
    print("Stage docs/erd.md and recommit.")
    sys.exit(1)


if __name__ == "__main__":
    main()
