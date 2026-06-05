from django.conf import settings
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

# Dev-only passwordless sign-in. Registered only under DEBUG; the view also
# hard-guards on DEBUG so it can never resolve in production.
if settings.DEBUG:
    urlpatterns += [
        path("dev-login/", views.dev_login, name="dev_login"),
    ]
