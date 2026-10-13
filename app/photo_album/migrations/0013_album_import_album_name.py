from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("photo_album", "0012_album_import_merge_into_existing"),
    ]

    operations = [
        migrations.AddField(
            model_name="albumimport",
            name="album_name",
            field=models.CharField(blank=True, max_length=140),
        ),
    ]
