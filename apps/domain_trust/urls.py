from django.urls import path

from . import views

app_name = "domain_trust"

urlpatterns = [
    path("", views.index, name="index"),
    path("<int:pk>/refresh/", views.refresh, name="refresh"),
    path("<int:pk>/verify-delivery/", views.verify_delivery, name="verify_delivery"),
    path("<int:pk>/delete/", views.delete, name="delete"),
]
