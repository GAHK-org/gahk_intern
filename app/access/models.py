"""The model the access permissions hang on. Django keeps every permission on a model; this one has
no table and no rows, only the permissions (access.permissions) in its Meta."""

from django.db import models

from .permissions import all_codenames


class AccessRule(models.Model):
    class Meta:
        managed = False
        default_permissions = ()
        permissions = all_codenames()
