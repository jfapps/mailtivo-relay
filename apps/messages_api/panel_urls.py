from django.urls import path

from . import panel_views

app_name = "messages_panel"

urlpatterns = [
    path("", panel_views.list_view, name="list"),
    path("test-inbox/", panel_views.list_view, {"sandbox": True}, name="test_inbox"),
    path("<str:message_id>/simulate/", panel_views.simulate_event_view, name="simulate"),
    path("<str:message_id>/", panel_views.detail_view, name="detail"),
]
