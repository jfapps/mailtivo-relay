from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="DomainIdentity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("domain", models.CharField(max_length=253, unique=True)),
                ("label", models.CharField(blank=True, max_length=120)),
                ("dkim_selector", models.CharField(blank=True, max_length=63)),
                (
                    "certificate_type",
                    models.CharField(
                        choices=[("none", "No certificate"), ("cmc", "CMC"), ("vmc", "VMC")],
                        default="none",
                        max_length=8,
                    ),
                ),
                ("latest_state", models.JSONField(blank=True, default=dict)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["domain"]},
        ),
    ]
