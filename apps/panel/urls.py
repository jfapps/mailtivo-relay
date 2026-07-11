from django.urls import path

from . import views

app_name = "panel"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("kpis/", views.kpis_partial, name="kpis"),
    path("settings/", views.settings_view, name="settings"),
    path("data/", views.data_view, name="data"),
    path("data/purge/", views.data_purge_now, name="data_purge_now"),
    path("data/purge-older/", views.data_purge_older, name="data_purge_older"),
    path("data/delete-captured/", views.data_delete_captured, name="data_delete_captured"),
    path("team/", views.team_view, name="team"),
    path("team/invitations/<int:invitation_id>/revoke/", views.invitation_revoke, name="invitation_revoke"),
    path("team/members/<int:user_id>/remove/", views.member_remove, name="member_remove"),
]
