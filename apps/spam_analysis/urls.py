from django.urls import path

from . import views

app_name = "integrations"

urlpatterns = [
    path("", views.integrations_view, name="index"),
    path("ai/test/", views.ai_test, name="ai_test"),
    path("safe-browsing/test/", views.safe_browsing_test, name="safe_browsing_test"),
]
