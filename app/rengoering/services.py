"""Søndagsrengøring — the rules that are not just a row: which tasks apply, and seeding the areas."""

from django.db import transaction
from django.db.models import QuerySet
from django.utils.text import slugify

from .areas_data import AREAS
from .models import CleaningArea, CleaningAssignment, CleaningTask

STAR_PREFIX = "*"


def visible_tasks(assignment: CleaningAssignment) -> "QuerySet[CleaningTask]":
    """The checklist for one assignment: the area's active tasks, (*)-tasks only in (*)-uger."""
    tasks = assignment.area.tasks.filter(is_active=True)
    if not assignment.is_star_week:
        tasks = tasks.filter(star_only=False)
    return tasks


@transaction.atomic
def load_areas(reset: bool = False) -> tuple[int, int]:
    """Create the areas in areas_data.AREAS that do not exist yet, with their tasks.

    An existing area is left as Inspektionen has edited it, unless `reset`, which replaces its task
    list with the seed's (old tasks are deactivated, not deleted, so history keeps its checkmarks).
    Returns (areas created, tasks created).
    """
    areas_created = tasks_created = 0
    for order, (name, description, aliases, zones) in enumerate(AREAS):
        area, created = CleaningArea.objects.get_or_create(
            slug=slugify(name),
            defaults={
                "name": name,
                "description": description,
                "import_aliases": "\n".join(aliases),
                "order": order,
            },
        )
        areas_created += created
        if not created and not reset:
            continue
        area.tasks.filter(is_active=True).update(is_active=False)
        position = 0
        for zone, groups in zones:
            for group, texts in groups:
                for raw in texts:
                    star = raw.startswith(STAR_PREFIX)
                    CleaningTask.objects.create(
                        area=area,
                        zone=zone,
                        group=group,
                        text=raw.removeprefix(STAR_PREFIX),
                        star_only=star,
                        order=position,
                    )
                    position += 1
                    tasks_created += 1
    return areas_created, tasks_created
