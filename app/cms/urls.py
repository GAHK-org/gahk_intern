"""The Hjemmeside editor, at /hjemmeside/. gahk.dk's "Rediger siden" links to sider/<id>/."""

from django.urls import path

from . import views

app_name = "hjemmeside"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("status/", views.status, name="status"),
    path("udgiv/", views.publish_now, name="publish_now"),
    path("opdater-gahk-dk/", views.update_now, name="update_now"),
    path("forhaandsvis/", views.preview, name="preview"),
    path("sider/", views.page_list, name="page_list"),
    path("sider/ny/", views.page_edit, name="page_create"),
    path("sider/<int:pk>/", views.page_edit, name="page_edit"),
    path("sider/<int:pk>/historik/", views.page_history, name="page_history"),
    path("sider/<int:pk>/historik/<int:version_pk>/gendan/", views.page_restore, name="page_restore"),
    path("sider/<int:pk>/slet/", views.page_delete, name="page_delete"),
    path("nyheder/", views.news_list, name="news_list"),
    path("nyheder/ny/", views.news_edit, name="news_create"),
    path("nyheder/<int:pk>/", views.news_edit, name="news_edit"),
    path("nyheder/<int:pk>/slet/", views.news_delete, name="news_delete"),
    path("begivenheder/", views.event_list, name="event_list"),
    path("begivenheder/ny/", views.event_edit, name="event_create"),
    path("begivenheder/<int:pk>/", views.event_edit, name="event_edit"),
    path("begivenheder/<int:pk>/slet/", views.event_delete, name="event_delete"),
    path("billeder/", views.image_list, name="image_list"),
    path("billeder/bibliotek.json", views.image_library_json, name="image_library_json"),
    path("billeder/upload.json", views.image_upload_json, name="image_upload_json"),
    path("billeder/<int:pk>/", views.image_detail, name="image_detail"),
    path("billeder/<int:pk>/slet/", views.image_delete, name="image_delete"),
    path("menu/", views.menu_edit, name="menu"),
    path("menu/standardbaggrund/", views.site_settings, name="site_settings"),
    path("omdirigeringer/", views.redirect_list, name="redirect_list"),
    path("omdirigeringer/<int:pk>/slet/", views.redirect_delete, name="redirect_delete"),
    path("aendringer/", views.change_log, name="change_log"),
]
