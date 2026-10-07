"""Kitchen shifts as VEVENTs for the resident's personal calendar feed (events.views.calendar_feed).

Reuses events.icalendar's escaping and UTC formatting so both kinds of entry obey the same rules.
"""

from events.icalendar import ICAL_DOMAIN, _escape, _utc

from .models import KitchenAssignment


def vevent(assignment: KitchenAssignment) -> list[str]:
    shift = assignment.shift
    stamp = max(shift.updated_at, assignment.updated_at)
    summary = f"Køkkenvagt: {shift.title}"
    if assignment.spots > 1:
        summary += f" ({assignment.spots} pladser)"
    lines = [
        "BEGIN:VEVENT",
        f"UID:koekkenvagt-{shift.pk}@{ICAL_DOMAIN}",
        f"DTSTAMP:{_utc(stamp)}",
        f"DTSTART:{_utc(shift.starts_at)}",
        f"DTEND:{_utc(shift.ends_at)}",
        f"SUMMARY:{_escape(summary)}",
        f"URL:https://{ICAL_DOMAIN}/intern/koekkenvagter/vagt/{shift.pk}",
        f"LAST-MODIFIED:{_utc(stamp)}",
        "LOCATION:Køkkenet",
    ]
    if shift.is_disabled:
        lines += ["STATUS:CANCELLED", "TRANSP:TRANSPARENT"]
        if shift.disabled_reason:
            lines.append(f"DESCRIPTION:{_escape('Aflyst: ' + shift.disabled_reason)}")
    else:
        lines += ["STATUS:CONFIRMED", "TRANSP:OPAQUE"]
    lines.append("END:VEVENT")
    return lines


def vevents_for(resident_id: int, since: object) -> list[str]:
    assignments = (
        KitchenAssignment.objects.filter(resident_id=resident_id, shift__starts_at__gte=since)
        .select_related("shift")
        .order_by("shift__starts_at")
    )
    return [line for a in assignments for line in vevent(a)]
