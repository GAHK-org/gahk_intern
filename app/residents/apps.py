from django.apps import AppConfig


class ResidentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "residents"

    def ready(self) -> None:
        from core.media import register_cacheable

        from . import auth, signals  # noqa: F401  (failed-login log; RoleAssignment → is_staff sync)

        # Avatars come from the server's disk, every one of them: a thumbnail is only ever named by
        # the resident it is the avatar of, and a replaced one is deleted, and its copy with it
        # (core.files). Pages like the alumneliste and Den Hurtige draw dozens per view, so no row is
        # looked up per request. Originals, which only the profile page opens, stay on the bucket.
        register_cacheable("profile_pictures/thumbnails/", lambda _name: True)
