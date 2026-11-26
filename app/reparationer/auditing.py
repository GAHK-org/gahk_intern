"""What the audit trail keeps of Reparationer: tickets, their notes and photos, and every move."""

from audit import trail

from .models import RepairComment, RepairImage, RepairTask

trail.register(
    RepairTask,
    "reparationer.task",
    name=lambda task: task.title,
    label="Reparation",
    # Timestamps kept by code: when a ticket entered its column follows each move of `status`, and
    # when it was archived is said by the archive events themselves (the nightly sweep included).
    ignore={"created_at", "updated_at", "status_changed_at", "archived_at"},
    labels={
        "title": "Titel",
        "description": "Beskrivelse",
        "location": "Sted",
        "status": "Status",
        "responsible": "Ansvarlig",
        "reported_by": "Meldt af",
    },
)
trail.register(
    RepairComment,
    "reparationer.comment",
    name=lambda comment: f"{comment.author.full_name}: {comment.body[:40]}",
    label="Note på reparation",
    ignore={"created_at"},
    labels={"task": "Reparation", "author": "Skrevet af", "body": "Note"},
)
trail.register(
    RepairImage,
    "reparationer.image",
    name=lambda image: image.file.name.rsplit("/", 1)[-1],
    label="Billede af reparation",
    ignore={"uploaded_at"},
    labels={"task": "Reparation", "comment": "Note", "file": "Fil"},
)

trail.actions(
    {
        "reparationer.task.reported": "Meldte en reparation ind",
        "reparationer.task.moved": "Flyttede reparationen",
        "reparationer.task.handed_over": "Overdrog reparationen",
        "reparationer.task.deleted": "Slettede reparationen",
        "reparationer.task.archived": "Arkiverede reparationen",
        "reparationer.task.unarchived": "Genåbnede reparationen fra arkivet",
        "reparationer.archive.swept": "Arkiverede færdige reparationer",
        "reparationer.comment.added": "Skrev en note på en reparation",
        "reparationer.comment.deleted": "Slettede en note på en reparation",
    }
)
