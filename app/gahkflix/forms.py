import json
from typing import Any

from django import forms
from django.core.exceptions import ValidationError

from photo_album.models import validate_tag_name
from residents.models import Resident

from .models import Series, Video, normalize_name

MAX_CREDITS = 100
MAX_TAGS = 20
# Digits only, and a number keypad on a phone. Text rather than type="number", which still lets
# "e", "+", "-" and "." in; gahkflix.ts strips anything else as it is typed.
NUMBER_ATTRS = {"inputmode": "numeric", "pattern": "[0-9]*", "autocomplete": "off"}


class VideoDetailsForm(forms.Form):
    """A video's details as one form. Cast and tags arrive as JSON from their editors (gahkflix.ts)."""

    title = forms.CharField(max_length=200, label="Titel")
    description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 5}), required=False, label="Beskrivelse"
    )
    recorded_on = forms.DateField(
        required=False, label="Optaget", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )
    series = forms.ModelChoiceField(
        queryset=Series.objects.all(), required=False, label="Serie", empty_label="Ingen serie"
    )
    season = forms.IntegerField(
        min_value=1,
        max_value=99,
        required=False,
        label="Sæson",
        widget=forms.TextInput(attrs={**NUMBER_ATTRS, "maxlength": 2, "data-flix-number": "season"}),
    )
    episode = forms.IntegerField(
        min_value=1,
        max_value=999,
        required=False,
        label="Afsnit",
        widget=forms.TextInput(attrs={**NUMBER_ATTRS, "maxlength": 3, "data-flix-number": "episode"}),
    )
    cast = forms.CharField(required=False, widget=forms.HiddenInput)
    tags = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args: Any, video: Video, **kwargs: Any) -> None:  # noqa: ANN401
        self.video = video
        super().__init__(*args, **kwargs)

    def clean_title(self) -> str:
        title = normalize_name(self.cleaned_data["title"].strip())
        if not title:
            raise ValidationError("Videoen skal have en titel.")
        return title

    def clean_cast(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.cleaned_data["cast"] or "[]")
        except ValueError as exc:
            raise ValidationError("Medvirkende kunne ikke læses.") from exc
        if not isinstance(raw, list) or len(raw) > MAX_CREDITS:
            raise ValidationError("Medvirkende kunne ikke læses.")
        ids = [item.get("resident") for item in raw if isinstance(item, dict) and item.get("resident")]
        known = dict(
            Resident.objects.filter(pk__in=[i for i in ids if isinstance(i, int)]).values_list(
                "pk", "first_name"
            )
        )
        cast = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            resident = item.get("resident") if item.get("resident") in known else None
            name = normalize_name(str(item.get("name", "")).strip())[:120]
            role = normalize_name(str(item.get("role", "")).strip())[:120]
            if resident is None and not name:
                continue
            cast.append({"resident": resident, "name": name, "role": role})
        return cast

    def clean_tags(self) -> list[str]:
        try:
            raw = json.loads(self.cleaned_data["tags"] or "[]")
        except ValueError as exc:
            raise ValidationError("Tags kunne ikke læses.") from exc
        if not isinstance(raw, list):
            raise ValidationError("Tags kunne ikke læses.")
        names: dict[str, str] = {}
        for value in raw:
            name = normalize_name(str(value).strip())
            validate_tag_name(name)
            names.setdefault(name.lower(), name)
        if len(names) > MAX_TAGS:
            raise ValidationError(f"Højst {MAX_TAGS} tags.")
        return sorted(names.values(), key=str.lower)

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        series, season, episode = data.get("series"), data.get("season"), data.get("episode")
        if series is None and (season or episode):
            raise ValidationError("Vælg en serie for at angive sæson og afsnit.")
        if series is not None and episode:
            clash = (
                Video.objects.filter(series=series, season=season, episode=episode, deleted_at__isnull=True)
                .exclude(pk=self.video.pk)
                .first()
            )
            if clash is not None:
                where = f"Sæson {season}, afsnit {episode}" if season else f"Afsnit {episode}"
                raise ValidationError(f"{where} findes allerede i {series}: {clash.title}.")
        return data

    def details(self) -> dict[str, Any]:
        data = self.cleaned_data
        return {
            "title": data["title"],
            "description": data["description"].strip(),
            "recorded_on": data["recorded_on"].isoformat() if data["recorded_on"] else None,
            "series": data["series"].pk if data["series"] else None,
            "season": data["season"],
            "episode": data["episode"],
            "cast": data["cast"],
            "tags": data["tags"],
        }


class SeriesForm(forms.ModelForm):
    class Meta:
        model = Series
        fields = ["title", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}

    def clean_title(self) -> str:
        title = normalize_name(self.cleaned_data["title"].strip())
        if not title:
            raise ValidationError("Serien skal have en titel.")
        clash = Series.objects.filter(title__iexact=title)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError("Der findes allerede en serie med den titel.")
        return title


class DeletionRequestForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=1000, label="Begrundelse")


class VoteForm(forms.Form):
    decision = forms.ChoiceField(choices=[("approve", "Godkend"), ("reject", "Afvis")])
    comment = forms.CharField(max_length=255, required=False, label="Kommentar")
