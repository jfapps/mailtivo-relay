from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("postal/<int:connection_id>/", views.postal_webhook, name="postal"),
    path("resend/<int:connection_id>/", views.resend_webhook, name="resend"),
    path("ses/<int:connection_id>/", views.ses_webhook, name="ses"),
]
