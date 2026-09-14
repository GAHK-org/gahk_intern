from django import forms

from core.models import Room

from .models import RepairComment, RepairTask

# Order matters: this is floor-by-floor building order, not alphabetical — Room.floor values are
# free text ("stuen", "1. sal", …), so nothing enforces this order except listing it here.
FLOORS = ["stuen", "1. sal", "2. sal", "3. sal", "4. sal"]

# Not exhaustive — a deliberate starting list (per the feature request) rather than a full survey of
# the building. Add to this as a missing area comes up; there is no user-facing "other" free-text
# escape hatch, so anywhere not listed needs a code change here before it can be selected.
COMMON_AREAS = [
    "Gammel Cykelkælder",
    "Vaskerummet",
    "Køkken Kælder",
    "Varme Køkken",
    "Kolde Køkken",
    "Køkken Kontor",
    "Køkken Indgang",
    "Spisesalen",
    "Festsalen",
    "Batik",
    "Ølkælderen",
    "Toilet Kælder",
    "Cykelkælderen",
    "Musiklokalet",
    "Skraldegården",
    "Terrassen",
    "Prison",
    "Varme Trappe",
    "Kolde Trappe",
    "Hallen",
    "Loungen",
    "Værkstedet",
    "Arkivet",
    "Læsesalen",
    "Svalegangen",
]


def location_choices() -> list[tuple[str, str] | tuple[str, list[tuple[str, str]]]]:
    """Grouped <select> choices: a fixed "Fællesområder" group for named common areas first, then
    every real room, floor by floor (each floor's own optgroup ending in that floor's gang,
    toilet, and køkken).

    Values are plain display strings, not room FKs: RepairTask.location stays a free CharField, so a
    location renamed or removed here later does not orphan any existing ticket's text.
    """
    groups: list[tuple[str, str] | tuple[str, list[tuple[str, str]]]] = [("", "— Vælg sted —")]
    groups.append(("Fællesområder", [(name, name) for name in COMMON_AREAS]))
    for floor in FLOORS:
        room_names = [str(room) for room in Room.objects.filter(floor=floor).order_by("number")]
        options = [(name, name) for name in room_names]
        options.append((f"Gang, {floor}", f"Gang, {floor}"))
        options.append((f"Toilet, {floor}", f"Toilet, {floor}"))
        options.append((f"Køkken, {floor}", f"Køkken, {floor}"))
        groups.append((floor.capitalize(), options))
    return groups


class RepairTaskForm(forms.ModelForm):
    # Declared explicitly (not left to ModelForm's auto-generation from the CharField) so it renders
    # as a <select>; choices are bound fresh in __init__, never at class-body eval time — the room
    # list is real DB data, not a fixed set (mirrors opslagstavle.forms.NoticeForm's `event` field).
    location = forms.ChoiceField(label="Sted", choices=(), required=False)

    class Meta:
        model = RepairTask
        fields = ["title", "location", "description"]
        labels = {"title": "Hvad er der galt?", "description": "Beskrivelse"}
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "F.eks. Utæt vandhane"}),
            "description": forms.Textarea(attrs={"rows": 5, "placeholder": "Beskriv problemet…"}),
        }

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.fields["location"].choices = location_choices()  # type: ignore[attr-defined]

    def clean_title(self) -> str:
        title = (self.cleaned_data.get("title") or "").strip()
        if not title:
            raise forms.ValidationError("Skriv hvad der er galt.")
        return title


class RepairCommentForm(forms.ModelForm):
    class Meta:
        model = RepairComment
        fields = ["body"]
        labels = {"body": "Note"}
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Skriv en note…"})}

    def clean_body(self) -> str:
        body = (self.cleaned_data.get("body") or "").strip()
        if not body:
            raise forms.ValidationError("Skriv en note.")
        return body
