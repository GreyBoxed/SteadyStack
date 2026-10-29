from django.urls import path

from rebalance import views

app_name = "rebalance"

urlpatterns = [
    path("", views.index, name="index"),
    path("template.csv", views.template, name="template"),
    path("<uuid:pk>/", views.detail, name="detail"),
    path("<uuid:pk>/execute/", views.execute, name="execute"),
    path("<uuid:pk>/cancel/", views.cancel, name="cancel"),
]
