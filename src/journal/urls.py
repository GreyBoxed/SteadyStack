from django.urls import path

from journal import views

app_name = "journal"

urlpatterns = [
    path("", views.index, name="index"),
    path("journal/", views.entries, name="entries"),
    path("ledger/<str:code>/", views.ledger, name="ledger"),
    path("entry/<uuid:pk>/", views.entry, name="entry"),
    path("entry/<uuid:pk>/reverse/", views.reverse_entry, name="reverse"),
    path("close/", views.close, name="close_year"),
    path("statement/", views.statement, name="statement"),
    path("statement/<int:pk>/confirm/", views.confirm_line, name="confirm"),
    path("statement/<int:pk>/match/", views.match_line, name="match"),
    path("statement/<int:pk>/correct/", views.correct, name="correct"),
    path("statement/<int:pk>/explain/", views.explain, name="explain"),
    path("dividend/", views.dividend, name="dividend"),
]
