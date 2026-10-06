from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("domain_trust", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="domainidentity",
            name="latest_delivery_state",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="domainidentity",
            name="last_delivery_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
