from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("", RedirectView.as_view(url="/app/", permanent=False)),
    path("", include("apps.accounts.urls")),
    path("app/", include("apps.panel.urls")),
    path("app/connections/", include("apps.connections.urls")),
    path("app/pools/", include("apps.pools.urls")),
    path("app/api-keys/", include("apps.api_keys.urls")),
    path("app/messages/", include("apps.messages_api.panel_urls")),
    path("api/v1/", include("apps.messages_api.urls")),
    path("webhooks/", include("apps.events.urls")),
    path("accounts/", include("allauth.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
