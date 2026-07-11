from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse
from django.urls import include, path
from django.views.generic import RedirectView


def healthz(_request):
    """Liveness probe used by docker-compose / k8s. Cheap on purpose — does
    not touch the database. Add a readiness probe later if you need one."""
    return JsonResponse({"ok": True})


urlpatterns = [
    path("", RedirectView.as_view(url="/app/", permanent=False)),
    path("healthz/", healthz, name="healthz"),
    path("", include("apps.accounts.urls")),
    path("app/", include("apps.panel.urls")),
    path("app/connections/", include("apps.connections.urls")),
    path("app/pools/", include("apps.pools.urls")),
    path("app/api-keys/", include("apps.api_keys.urls")),
    path("app/messages/", include("apps.messages_api.panel_urls")),
    path("app/suppressions/", include("apps.suppressions.urls")),
    path("app/webhooks/", include("apps.events.panel_urls")),
    path("app/tools/", include("apps.tools.urls")),
    path("app/integrations/", include("apps.spam_analysis.urls")),
    path("app/audit/", include("apps.audit.urls")),
    path("api/v1/", include("apps.messages_api.urls")),
    path("webhooks/", include("apps.events.urls")),
    path("accounts/", include("allauth.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
