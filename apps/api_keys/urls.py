from django.urls import path

from . import views

app_name = "api_keys"

urlpatterns = [
    path("", views.list_view, name="list"),
    path("<int:pk>/pool/", views.set_pool_view, name="set_pool"),
    path("<int:pk>/revoke/", views.revoke_view, name="revoke"),
]
