from django.urls import path

from web import views

app_name = "web"

urlpatterns = [
    path("", views.home, name="home"),
    path("live/today/", views.home_live, name="home_live"),
    path("holdings/", views.holdings, name="holdings"),
    path("performance/", views.performance, name="performance"),
    path("orders/today/", views.orders, name="orders"),
    path("live/orders/", views.orders_live, name="orders_live"),
    path("orders/<slug:day>/<str:symbol>/", views.instrument_day, name="instrument_day"),
    path(
        "live/depth/<slug:day>/<str:symbol>/",
        views.instrument_depth_live,
        name="instrument_depth_live",
    ),
    path("ledger/cash/", views.cash_ledger, name="cash_ledger"),
    path("ledger/shares/", views.share_ledger, name="share_ledger"),
]
