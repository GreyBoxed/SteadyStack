from django.urls import path

from broker import session_view, views

app_name = "broker"

urlpatterns = [
    path("session/", session_view.session, name="session"),
    path("kite/login/", views.start_login, name="start_login"),
    # Exactly the path the OTP allowlist exempts, and the one registered with
    # Zerodha as the redirect URL.
    path("kite/callback/", views.callback, name="callback"),
]
