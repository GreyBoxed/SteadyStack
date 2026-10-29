from django.urls import path

from engine import views

app_name = "engine"

urlpatterns = [
    path("", views.index, name="index"),
    path("today/generate/", views.generate_today, name="generate"),
    path("<slug:trading_date>/v<int:version>/authorize/", views.authorize, name="authorize"),
    path("<slug:trading_date>/v<int:version>/", views.detail, name="detail"),
    path("<slug:trading_date>/v<int:version>/override/", views.override, name="override"),
    path("<slug:trading_date>/v<int:version>/regenerate/", views.regenerate, name="regenerate"),
    path("<slug:trading_date>/v<int:version>/replan/", views.replan, name="replan"),
]
