from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("photo_album", "0011_tags"),
    ]

    operations = [
        migrations.AddField(
            model_name="albumimport",
            name="merge_into_existing",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="albumimport",
            name="merged",
            field=models.BooleanField(default=False),
        ),
    ]
