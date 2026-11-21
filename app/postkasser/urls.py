"""Mounted at /postkasser/ from residents/urls.py."""

from django.urls import path

from . import views

app_name = "postkasser"

urlpatterns = [
    path("", views.index, name="index"),
    path("<slug:slug>/", views.board, name="board"),
    path("<slug:slug>/hent", views.refresh, name="refresh"),
    path("<slug:slug>/ny", views.compose, name="compose"),
    path("<slug:slug>/arkiv", views.archive, name="archive"),
    path("<slug:slug>/kolonner", views.board_columns, name="columns"),
    path("<slug:slug>/tags", views.tag_page, name="tags"),
    path("<slug:slug>/automation", views.automation, name="automation"),
    path("<slug:slug>/sag/<int:pk>", views.thread, name="thread"),
    path("<slug:slug>/sag/<int:pk>/status", views.set_status, name="set_status"),
    path("<slug:slug>/sag/<int:pk>/tags", views.set_tags, name="set_tags"),
    path("<slug:slug>/sag/<int:pk>/ansvarlig", views.set_assignee, name="set_assignee"),
    path("<slug:slug>/sag/<int:pk>/flyt", views.move_thread_mail, name="move_thread"),
    path("<slug:slug>/sag/<int:pk>/ulaest", views.thread_unread, name="thread_unread"),
    path("<slug:slug>/<int:pk>", views.message, name="message"),
    path("<slug:slug>/<int:pk>/flyt", views.move_message, name="move_message"),
    path("<slug:slug>/<int:pk>/ulaest", views.message_unread, name="message_unread"),
    path("<slug:slug>/<int:pk>/svar", views.reply, name="reply"),
    path("<slug:slug>/<int:pk>/vedhaeftning/<int:index>", views.attachment, name="attachment"),
]
