from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("onboarding/", views.onboarding, name="onboarding"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("login/magic/", views.magic_link_request, name="magic_link_request"),
    path("login/magic/<str:token>/", views.magic_link_consume, name="magic_link_consume"),
    path("invitations/<str:token>/", views.invitation_accept, name="invitation_accept"),
]
