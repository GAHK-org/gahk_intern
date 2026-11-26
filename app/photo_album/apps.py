from django.apps import AppConfig


class PhotoAlbumConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "photo_album"
    verbose_name = "Fotoalbum"
    # Storage names this app owns; only its code writes under them (spec/architecture.md).
    storage_prefixes = ("photo-album/", "photo-album-imports/", "photo-album-zips/")
