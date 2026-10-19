"""Public application forms (F-001).

Explicit fields (kills the legacy mass-assignment), server-side validation, a honeypot for spam.
(reCAPTCHA/Turnstile to be wired with keys — see TODO in views.) gender is male/female/other.
"""

from typing import Literal

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
    # honeypot — real users leave this empty; bots fill it
    website = forms.CharField(required=False, widget=forms.HiddenInput)
    heard_about_us = forms.ChoiceField(choices=HEARD_CHOICES, required=True, label="Hvor hørte du om os?")
    gender = forms.ChoiceField(choices=Application.Gender.choices, required=True, label="Køn")

    def clean_website(self) -> Literal[""]:
        if self.cleaned_data.get("website"):
            raise forms.ValidationError("Spam detected.")
        return ""


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
