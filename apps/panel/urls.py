from django.urls import path

from . import views

app_name = "panel"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("kpis/", views.kpis_partial, name="kpis"),
    path("settings/", views.settings_view, name="settings"),
    path("team/", views.team_view, name="team"),
    path("team/invitations/<int:invitation_id>/revoke/", views.invitation_revoke, name="invitation_revoke"),
    path("team/members/<int:user_id>/remove/", views.member_remove, name="member_remove"),
]
