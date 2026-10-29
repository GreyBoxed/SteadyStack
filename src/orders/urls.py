from django.urls import path

from orders import views

app_name = "orders"

urlpatterns = [
    path("kite/postback/", views.kite_postback, name="kite_postback"),
]
