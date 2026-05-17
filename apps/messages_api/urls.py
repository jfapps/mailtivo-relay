from django.urls import path

from . import views

app_name = "messages_api"

urlpatterns = [
    path("emails", views.emails_create, name="emails_create"),
    path("emails/<str:message_id>", views.emails_retrieve, name="emails_retrieve"),
]
