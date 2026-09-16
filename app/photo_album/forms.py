from django import forms

from .models import Album
from .uploads import validate_media_upload


class AlbumForm(forms.ModelForm):
    class Meta:
        model = Album
        fields = ["folder", "name"]


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data: object, initial: object | None = None) -> list[object]:
        files = data if isinstance(data, list) else [data]
        # `required` has to be enforced here, not by FileField. With allow_multiple_selected the
        # widget hands us files.getlist(), so "nothing picked" arrives as [] rather than None — the
        # comprehension below then runs zero times and the field validates happily with no files.
        if not files and self.required:
            raise forms.ValidationError(self.error_messages["required"], code="required")
        return [super().clean(file, initial) for file in files]


class MediaUploadForm(forms.Form):
    # Per-file: Field.clean runs its validators, and MultipleFileField.clean calls it once per file.
    uploads = MultipleFileField(label="Fotoer eller videoer", validators=[validate_media_upload])
    title = forms.CharField(max_length=255, required=False, label="Titel")
