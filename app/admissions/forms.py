"""The field rules for an application (F-001): what a tour (rundvisning) or sublet (fremleje)
request must contain to be stored.

The forms people fill in are on gahk.dk (gahk_web), which keeps the same fields and choices. Its
submissions reach intern as untrusted data in the outbox, and admissions.services validates every
one with these forms before anything is stored — exactly as if it had been posted here. Spam
protection (honeypot, Turnstile) is gahk.dk's job now. gender is male/female/other.
"""

from django import forms

from .models import Application

HEARD_CHOICES = [
    ("", "— Vælg —"),
    ("sociale medier", "På sociale medier (Facebook eller Instagram)"),
    ("hjemmeside", "På en hjemmeside"),
    ("beboere", "Fra beboere der fortalte om kollegiet"),
    ("avis", "Annonce i avis"),
    ("anbefaling", "Anbefalet af en jeg kender"),
    ("plakat", "Set en plakat"),
    ("selv", "Selv fundet frem til det"),
]


class _BaseApplicationForm(forms.ModelForm):
    heard_about_us = forms.ChoiceField(choices=HEARD_CHOICES, required=True, label="Hvor hørte du om os?")
    gender = forms.ChoiceField(choices=Application.Gender.choices, required=True, label="Køn")


class RundvisningForm(_BaseApplicationForm):
    motivation = forms.CharField(
        label="Motivation",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
    )

    class Meta:
        model = Application
        fields = [
            "full_name",
            "email",
            "gender",
            "age",
            "study_year",
            "year_left",
            "university",
            "field_of_study",
            "heard_about_us",
            "motivation",
        ]
        labels = {
            "full_name": "Fulde navn",
            "email": "E-mail",
            "age": "Alder",
            "study_year": "Antal år studeret",
            "year_left": "Studieår tilbage",
            "university": "Universitet",
            "field_of_study": "Studieretning",
        }


class FremlejeForm(_BaseApplicationForm):
    class Meta:
        model = Application
        fields = ["full_name", "email", "gender", "age", "occupation", "heard_about_us", "motivation"]
        labels = {
            "full_name": "Fulde navn",
            "email": "E-mail",
            "age": "Alder",
            "occupation": "Beskæftigelse",
            "motivation": "Motivation",
        }
        widgets = {"motivation": forms.Textarea(attrs={"rows": 5})}
