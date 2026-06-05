import logging

from django.apps import AppConfig
from django.db.models.signals import post_migrate

log = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # No sender filter: apps.core has no models module, so a sender=self
        # filter would never fire. get_or_create below is idempotent, so running
        # once per migrated app is harmless.
        post_migrate.connect(_ensure_update_check_schedule)


def _ensure_update_check_schedule(**_kwargs) -> None:
    """Idempotently register the daily update check on django-q.

    Runs after each migrate (the container entrypoint migrates on every deploy).
    Best-effort: the Schedule table may not exist yet on a first-ever migrate.
    """
    try:
        from django_q.models import Schedule

        Schedule.objects.get_or_create(
            name="mailtivo-update-check",
            defaults={
                "func": "apps.core.updates.check_for_update",
                "schedule_type": Schedule.DAILY,
            },
        )
    except Exception as exc:  # noqa: BLE001 — schedule registration is best-effort
        log.debug("update-check schedule not registered: %s", exc)
