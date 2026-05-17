from django.urls import path

from . import views

app_name = "webhooks_panel"

urlpatterns = [
    path("", views.webhooks_list, name="list"),
    path("new/", views.webhooks_create, name="create"),
    path("<int:pk>/", views.webhooks_edit, name="edit"),
    path("<int:pk>/rotate/", views.webhooks_rotate, name="rotate"),
    path("<int:pk>/delete/", views.webhooks_delete, name="delete"),
]
