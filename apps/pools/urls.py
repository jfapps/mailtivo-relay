from django.urls import path

from . import views

app_name = "pools"

urlpatterns = [
    path("", views.list_view, name="list"),
    path("new/", views.create_view, name="create"),
    path("<int:pk>/", views.edit_view, name="edit"),
    path("<int:pk>/delete/", views.delete_view, name="delete"),
    path("<int:pool_id>/members/add/", views.member_add, name="member_add"),
    path("<int:pool_id>/members/<int:member_id>/", views.member_update, name="member_update"),
    path("<int:pool_id>/members/<int:member_id>/remove/", views.member_remove, name="member_remove"),
    path("<int:pool_id>/warmup/", views.warmup_save, name="warmup_save"),
]
