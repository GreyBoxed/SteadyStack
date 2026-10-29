from django.contrib.auth import views as auth_views
from django.urls import path

from accounts import views_security
from accounts.views import OperatorLoginView

app_name = "accounts"

urlpatterns = [
    path("security/", views_security.security, name="security"),
    path("security/reset-totp/", views_security.reset_totp, name="reset_totp"),
    path("login/", OperatorLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
]
