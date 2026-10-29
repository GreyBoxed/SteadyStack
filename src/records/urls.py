from django.urls import path

from records import views

app_name = "records"

urlpatterns = [
    path("divergences/", views.divergences, name="divergences"),
    path("divergences/<uuid:pk>/", views.classify, name="classify"),
    path("basis/", views.basis, name="basis"),
    path("corporate-actions/", views.corporate_action, name="corporate_action"),
]
