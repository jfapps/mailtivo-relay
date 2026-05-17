from django.urls import path

from . import views

app_name = "suppressions"

urlpatterns = [
    path("", views.list_view, name="list"),
    path("export.csv", views.export_view, name="export"),
    path("<int:pk>/remove/", views.remove_view, name="remove"),
]
