from django.urls import path

from model_portfolio import views

app_name = "model_portfolio"

urlpatterns = [
    path("", views.index, name="index"),
    path("upload/", views.upload, name="upload"),
    path("confirm/", views.confirm, name="confirm"),
]
