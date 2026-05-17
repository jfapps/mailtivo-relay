from django.urls import path

from . import panel_views

app_name = "messages_panel"

urlpatterns = [
    path("", panel_views.list_view, name="list"),
    path("<str:message_id>/", panel_views.detail_view, name="detail"),
]
