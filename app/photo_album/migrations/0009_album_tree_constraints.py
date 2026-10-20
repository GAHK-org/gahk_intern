import uuid

import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("photo_album", "0008_album_tree_and_media_identity_data")]

    operations = [
        migrations.AlterField(
            model_name="media",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddConstraint(
            model_name="album",
            constraint=models.UniqueConstraint(
                models.F("folder"),
                django.db.models.functions.text.Lower("name"),
                condition=models.Q(("parent__isnull", True)),
                name="uniq_root_album_name",
                violation_error_message="Der findes allerede et album med det navn i mappen.",
            ),
        ),
        migrations.AddConstraint(
            model_name="album",
            constraint=models.UniqueConstraint(
                models.F("parent"),
                django.db.models.functions.text.Lower("name"),
                condition=models.Q(("parent__isnull", False)),
                name="uniq_sub_album_name",
                violation_error_message="Der findes allerede et underalbum med det navn.",
            ),
        ),
        migrations.AddConstraint(
            model_name="album",
            constraint=models.CheckConstraint(condition=models.Q(("depth__lte", 4)), name="album_max_depth"),
        ),
    ]
