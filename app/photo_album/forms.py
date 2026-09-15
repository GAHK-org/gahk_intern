from django import forms

from .models import Album


class AlbumForm(forms.ModelForm):
    class Meta:
        model = Album
        fields = ["folder", "name"]


class MediaUploadForm(forms.Form):
    file = forms.FileField(label="Foto eller video")
    title = forms.CharField(max_length=255, required=False, label="Titel")
