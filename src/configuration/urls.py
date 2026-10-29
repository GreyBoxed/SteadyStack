from django.urls import path

from configuration import views

app_name = "configuration"

urlpatterns = [
    path("", views.index, name="index"),
    path("stop/", views.stop, name="stop"),
    path("resume/", views.resume, name="resume"),
]
