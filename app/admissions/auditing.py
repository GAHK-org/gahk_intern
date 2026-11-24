"""What the audit trail keeps of admission applications (Optagelse).

An application is deleted a year after it came in (purge_applications); the trail keeps events five
years. So the trail names an application by its kind and number only, and keeps none of what the
applicant wrote: their name, address, age and the rest stay in the application, and go with it.
What the trail holds is what Indstillingen did with it, and when.
"""

from audit import trail

from .models import Application

trail.register(
    Application,
    "admissions.application",
    name=lambda app: f"{app.get_type_display()} #{app.pk}",
    label="Ansøgning",
    # Everything the applicant typed, and gahk.dk's id for the submission: personal data, or a key
    # to it, that must not outlive the application in the trail.
    ignore={
        "full_name",
        "email",
        "gender",
        "age",
        "study_year",
        "year_left",
        "university",
        "field_of_study",
        "occupation",
        "heard_about_us",
        "motivation",
        "external_id",
    },
    labels={
        "type": "Type",
        "submitted_at": "Indsendt",
        "received_by": "Modtaget af",
        "received_at": "Modtaget",
        "discarded_by": "Kasseret af",
        "discarded_at": "Kasseret",
    },
)

trail.actions(
    {
        "admissions.application.submitted": "Ansøgning kom ind fra gahk.dk",
        "admissions.application.marked_received": "Markerede ansøgningen som modtaget",
        "admissions.application.discarded": "Kasserede ansøgningen",
        "admissions.application.undiscarded": "Fortrød kassering af ansøgningen",
        "admissions.application.purged": "Slettede ansøgninger over et år gamle",
    }
)
