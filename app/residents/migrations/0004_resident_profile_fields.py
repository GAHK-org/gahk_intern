# Generated manually 2026-08-27

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("residents", "0003_alter_roleassignment_role"),
    ]

    operations = [
        migrations.AddField(
            model_name="resident",
            name="profile_picture",
            field=models.FileField(blank=True, upload_to="profile_pictures/"),
        ),
        migrations.AddField(
            model_name="resident",
            name="bio",
            field=models.TextField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="resident",
            name="facebook_link",
            field=models.URLField(blank=True),
        ),
        migrations.AddField(
            model_name="resident",
            name="instagram_handle",
            field=models.CharField(blank=True, max_length=50),
        ),
    ]
