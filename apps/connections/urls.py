from django.urls import path

from . import views

app_name = "connections"

urlpatterns = [
    path("", views.list_view, name="list"),
    path("new/", views.create_view, name="create"),
    path("<int:pk>/", views.edit_view, name="edit"),
    path("<int:pk>/test/", views.test_view, name="test"),
    path("<int:pk>/delete/", views.delete_view, name="delete"),
]
