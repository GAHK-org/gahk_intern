import json
from datetime import datetime, time
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.danish import MONTHS
from photo_album.models import validate_tag_name
from residents.models import Resident

from .dating import FIRST_YEAR, DatePrecision, PartialDate, parse_premiere, premiere_value
from .models import PREMIERE_MAX_AHEAD, PremiereBlur, Series, Video, normalize_name

MAX_CREDITS = 100
MAX_TAGS = 20
# Digits only, and a number keypad on a phone. Text rather than type="number", which still lets
# "e", "+", "-" and "." in; gahkflix.ts strips anything else as it is typed.
NUMBER_ATTRS = {"inputmode": "numeric", "pattern": "[0-9]*", "autocomplete": "off"}


class VideoDetailsForm(forms.Form):
    """A video's details as one form. Cast and tags arrive as JSON from their editors (gahkflix.ts).

    The release date is three fields, of which only the year is needed: a month and a day nobody
    knows are left out rather than made up (dating.py). It cannot be in the future — unless the video
    is a premiere (`premiere`), which comes out at a whole moment: the day, month and year, and a
    time (`premiere_time`), which only a premiere has.
    """

    title = forms.CharField(max_length=200, label="Titel")
    description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 5}), required=False, label="Beskrivelse"
    )
    released_year = forms.IntegerField(
        min_value=FIRST_YEAR,
        required=False,
        label="År",
        widget=forms.TextInput(
            attrs={**NUMBER_ATTRS, "maxlength": 4, "placeholder": "År", "aria-label": "År"}
        ),
    )
    released_month = forms.TypedChoiceField(
        choices=[("", "Måned"), *((number, MONTHS[number]) for number in range(1, 13))],
        coerce=int,
        empty_value=None,
        required=False,
        label="Måned",
        widget=forms.Select(attrs={"aria-label": "Måned"}),
    )
    released_day = forms.TypedChoiceField(
        choices=[("", "Dag"), *((number, f"{number}.") for number in range(1, 32))],
        coerce=int,
        empty_value=None,
        required=False,
        label="Dag",
        widget=forms.Select(attrs={"aria-label": "Dag"}),
    )
    premiere = forms.BooleanField(required=False, label="Premiere")
    premiere_time = forms.TimeField(
        required=False,
        label="Tidspunkt",
        input_formats=["%H:%M", "%H.%M"],
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time", "aria-label": "Tidspunkt"}),
    )
    premiere_blur = forms.ChoiceField(
        choices=PremiereBlur.choices,
        required=False,
        initial=PremiereBlur.NONE,
        label="Miniature før premieren",
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
        self.release: PartialDate | None = None
        self.premiere_at: datetime | None = None
        super().__init__(*args, **kwargs)

    @staticmethod
    def release_initial(value: str | None, premiere: str | None = None) -> dict[str, Any]:
        """The date fields for a release date and a premiere as services.snapshot has them ("2015-03",
        "2026-12-24T20:00+01:00")."""
        release = PartialDate.parse(value)
        moment = parse_premiere(premiere)
        return {
            "released_year": release.year if release else None,
            "released_month": release.month if release else None,
            "released_day": release.day if release else None,
            "premiere": moment is not None,
            "premiere_time": timezone.localtime(moment).time() if moment else None,
        }

    def clean_premiere_blur(self) -> str:
        """Left out, it stays as it is."""
        return self.cleaned_data["premiere_blur"] or self.video.premiere_blur

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

    def _clean_premiere(self, data: dict[str, Any]) -> None:
        year, month, day = (data.get(f"released_{part}") for part in ("year", "month", "day"))
        at: time | None = data.get("premiere_time")
        if not (year and month and day):
            self.add_error("premiere", "En premiere skal have dag, måned og år.")
        if at is None:
            if "premiere_time" not in self.errors:
                self.add_error("premiere_time", "Vælg også klokkeslættet for premieren.")
            return
        if not (year and month and day):
            return
        try:
            moment = timezone.make_aware(datetime.combine(PartialDate.of(year, month, day).start, at))
        except ValueError:
            self.add_error("released_day", f"Der er ingen {day}. {MONTHS[month]} i {year}.")
            return
        now = timezone.now()
        if moment > now + PREMIERE_MAX_AHEAD:
            self.add_error("premiere", "Premieren kan højst ligge et år ude i fremtiden.")
        elif moment > now and self.video.is_released and not self.video.can_be_taken_back:
            self.add_error(
                "premiere",
                "Videoen har været ude i mere end et døgn, så den kan ikke længere få premiere senere.",
            )
        else:
            self.premiere_at = moment
            self.release = PartialDate(moment.date(), DatePrecision.DAY)

    def _clean_release(self, data: dict[str, Any]) -> None:
        if data.get("premiere"):
            self._clean_premiere(data)
            return
        year, month, day = (data.get(f"released_{part}") for part in ("year", "month", "day"))
        if day and not month:
            self.add_error("released_day", "Vælg også måneden, eller lad dagen stå tom.")
        elif (month or day) and not year:
            if "released_year" not in self.errors:
                self.add_error("released_year", "Skriv også året.")
        elif year:
            try:
                self.release = PartialDate.of(year, month, day)
            except ValueError:
                # Only a day its month does not have gets here: a day comes with a month (above).
                self.add_error("released_day", f"Der er ingen {day}. {MONTHS[month or 0]} i {year}.")
                return
            if self.release.start > timezone.localdate():
                self.release = None
                self.add_error(
                    "released_year",
                    "Udgivelsesdatoen ligger i fremtiden. Sæt flueben ved Premiere, hvis videoen skal "
                    "udkomme på et bestemt tidspunkt.",
                )

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        self._clean_release(data)
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
            "released_on": self.release.iso() if self.release else None,
            "premiere_at": premiere_value(self.premiere_at),
            "premiere_blur": data["premiere_blur"],
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
