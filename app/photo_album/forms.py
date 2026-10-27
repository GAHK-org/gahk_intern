import zipfile

from django import forms
from django.core.exceptions import ValidationError

from .models import (
    FOLDER_MAX_LENGTH,
    Album,
    DownloadVariant,
    normalize_name,
    validate_album_name,
    validate_folder,
)
from .uploads import validate_media_upload


class AlbumForm(forms.ModelForm):
    class Meta:
        model = Album
        fields = ["folder", "name"]


class AlbumNameForm(forms.Form):
    """Naming a sub-album, or renaming any album — the folder comes from the tree, not the form."""

    name = forms.CharField(max_length=140, label="Navn", validators=[validate_album_name])


class AlbumMergeForm(forms.Form):
    """The album to merge into this one; which albums may be is merge.check's to say."""

    source = forms.ModelChoiceField(
        queryset=Album.objects.all(),
        error_messages={
            "required": "Vælg det album, der skal flettes ind.",
            "invalid_choice": "Det album findes ikke længere.",
        },
    )


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


def _clean_folder(value: object) -> str:
    """A folder as it is stored: a year, or NO_YEAR_FOLDER in NFC, however a Mac spelled its "å"."""
    folder = normalize_name(str(value))
    validate_folder(folder)
    return folder


class ZipAlbumImportForm(forms.Form):
    folder = forms.CharField(max_length=FOLDER_MAX_LENGTH, label="Mappe")
    archive = forms.FileField(label="ZIP-fil")

    def clean_folder(self) -> str:
        return _clean_folder(self.cleaned_data["folder"])

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


class SystemZipImportForm(ZipAlbumImportForm):
    """The import script's upload. `album` pins the root album: the archive's paths are then relative
    to it, rather than the album being named after the ZIP and the folders it descends through."""

    album = forms.CharField(max_length=140, required=False, validators=[validate_album_name])


class ImportUploadForm(forms.Form):
    """Asking to send an archive straight to the bucket (photo_album.direct_upload): the import
    script, or the import page in a browser."""

    name = forms.CharField(max_length=200)
    size = forms.IntegerField(min_value=1)

    def clean_name(self) -> str:
        name = str(self.cleaned_data["name"])
        if not name.lower().endswith(".zip"):
            raise ValidationError("Vælg en ZIP-fil.")
        return name


def _clean_parts(parts: object) -> list[str]:
    if not isinstance(parts, list) or not parts or not all(isinstance(etag, str) for etag in parts):
        raise ValidationError("Delene skal være en liste af ETags.")
    return parts


class DirectZipImportForm(forms.Form):
    """An import of an archive already in the bucket: the upload's ticket and the ETags of its
    parts, in order, instead of the file."""

    folder = forms.CharField(max_length=FOLDER_MAX_LENGTH, label="Mappe")
    album = forms.CharField(max_length=140, required=False, validators=[validate_album_name])
    ticket = forms.CharField()
    # The import script's parts, which the server completes the upload from; a browser's Uppy
    # completes its own and sends none.
    parts = forms.JSONField(required=False)

    def clean_folder(self) -> str:
        return _clean_folder(self.cleaned_data["folder"])

    def clean_parts(self) -> list[str]:
        parts = self.cleaned_data["parts"]
        return _clean_parts(parts) if parts else []


class MediaUploadBeginForm(forms.Form):
    """Asking to send one photo or video straight to the bucket; checked like the file itself would
    be by name, type and size before a byte is sent."""

    name = forms.CharField(max_length=255)
    size = forms.IntegerField(min_value=1)
    content_type = forms.CharField(max_length=100, required=False)


class MediaUploadCommitForm(forms.Form):
    """The photo or video sent, by its ticket, and what the upload form sends with every file."""

    ticket = forms.CharField()
    title = forms.CharField(max_length=255, required=False)
    last_modified = forms.CharField(required=False)


class DirectUploadSignForm(forms.Form):
    """One request Uppy wants signed (@uppy/aws-s3's PresignableRequest), and the ticket it is for."""

    ticket = forms.CharField()
    method = forms.ChoiceField(choices=[(method, method) for method in ("POST", "PUT", "DELETE")])
    key = forms.CharField(max_length=500)
    upload_id = forms.CharField(max_length=500, required=False)
    part_number = forms.IntegerField(min_value=1, required=False)


class SystemAlbumRenameForm(forms.Form):
    """The import script renaming a root album it imported: `album` in `folder` becomes `name`."""

    folder = forms.CharField(max_length=FOLDER_MAX_LENGTH)
    album = forms.CharField(max_length=140)
    name = forms.CharField(max_length=140, validators=[validate_album_name])

    def clean_folder(self) -> str:
        return _clean_folder(self.cleaned_data["folder"])


class SystemSidecarDatesForm(ZipAlbumImportForm):
    """The import script's sidecars for an album already imported, paths relative to `album`."""

    album = forms.CharField(max_length=140, validators=[validate_album_name])
