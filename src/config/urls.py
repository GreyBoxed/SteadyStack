from django.urls import include, path

from web import views as web_views

# No /admin/: nothing of ours is registered there, and what django-otp
# registers -- the authenticator devices -- must have no second way in.
urlpatterns = [
    path("accounts/", include("accounts.urls", namespace="accounts")),
    # Exact path, matching the OTP allowlist entry: /health/ being open must
    # not open anything that merely starts with it.
    path("health/", web_views.health, name="health"),
    path("broker/", include("broker.urls", namespace="broker")),
    path("orders/", include("orders.urls", namespace="orders")),
    path("tradesheets/", include("engine.urls", namespace="engine")),
    path("model-portfolio/", include("model_portfolio.urls", namespace="model_portfolio")),
    path("configuration/", include("configuration.urls", namespace="configuration")),
    path("books/", include("journal.urls", namespace="journal")),
    path("records/", include("records.urls", namespace="records")),
    path("rebalance/", include("rebalance.urls", namespace="rebalance")),
    path("instruments/", include("portfolio.urls", namespace="portfolio")),
    path("m/", include("web.phone_urls", namespace="phone")),
    path("", include("web.urls", namespace="web")),
]
