from django import forms

from .models import Album


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
        return [super().clean(file, initial) for file in files]


class MediaUploadForm(forms.Form):
    uploads = MultipleFileField(label="Fotoer eller videoer")
    title = forms.CharField(max_length=255, required=False, label="Titel")
