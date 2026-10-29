from django.urls import path

from web import phone

app_name = "phone"

urlpatterns = [
    path("", phone.today, name="today"),
    path("live/", phone.today_live, name="today_live"),
    path("login/", phone.PhoneLoginView.as_view(), name="login"),
    path("switch/", phone.switch, name="switch"),
    path("trade/", phone.trade, name="trade"),
    path("kite/", phone.kite, name="kite"),
    path("stop/", phone.stop, name="stop"),
]
