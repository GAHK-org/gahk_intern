import zipfile

from django import forms
from django.core.exceptions import ValidationError

from .models import Album, DownloadVariant, validate_album_name
from .uploads import validate_media_upload


class AlbumForm(forms.ModelForm):
    class Meta:
        model = Album
        fields = ["folder", "name"]


class AlbumNameForm(forms.Form):
    """Naming a sub-album, or renaming any album — the folder comes from the tree, not the form."""

    name = forms.CharField(max_length=140, label="Navn", validators=[validate_album_name])


class DownloadForm(forms.Form):
    variant = forms.ChoiceField(choices=DownloadVariant.choices, required=False)

    # "0" leaves the sub-albums out. Anything else, including nothing at all, downloads the whole
    # subtree — what "the entire album" meant before the choice existed.
    sub_albums = forms.ChoiceField(
        choices=[("1", "Med underalbummer"), ("0", "Kun dette album")], required=False
    )

    def clean_variant(self) -> str:
        return self.cleaned_data["variant"] or DownloadVariant.ORIGINAL

    def clean_sub_albums(self) -> bool:
        return self.cleaned_data["sub_albums"] != "0"


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


class ZipAlbumImportForm(forms.Form):
    folder = forms.CharField(max_length=10, label="Mappe")
    archive = forms.FileField(label="ZIP-fil")

    def clean_folder(self) -> str:
        folder = self.cleaned_data["folder"]
        if folder != "Andet" and not (len(folder) == 4 and folder.isdecimal()):
            raise ValidationError("Mappen skal være et årstal eller Andet.")
        return folder

    def clean_archive(self) -> object:
        archive = self.cleaned_data["archive"]
        if not getattr(archive, "name", "").lower().endswith(".zip"):
            raise ValidationError("Vælg en ZIP-fil.")
        try:
            archive.seek(0)
            with zipfile.ZipFile(archive):
                pass
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValidationError("ZIP-filen kunne ikke læses.") from exc
        finally:
            archive.seek(0)
        return archive
