"""Internal members area + auth, under /intern/. F-013/F-014."""

from django.contrib.auth import views as auth_views
from django.urls import include, path

from . import views, views_regnskab
from .forms import EmailAuthenticationForm, ResidentPasswordResetForm

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("regnskab/", views_regnskab.overview, name="regnskab"),
    path("alumneliste/", views.directory, name="directory"),
    path("alumneliste/rows", views.directory_rows, name="directory_rows"),
    path("alumneliste/eksport", views.directory_export, name="directory_export"),
    path("alumneliste/naeste-maaned", views.next_month_list, name="next_month_list"),
    path("beboer/<int:pk>/rediger", views.edit_resident, name="edit_resident"),
    path("beboer/<int:pk>/profil", views.profile, name="resident_profile"),
    path("beboer/min-profil/rediger", views.edit_profile, name="edit_profile"),
    path("stamtree/", views.stamtree, name="stamtree"),
    path("den-hurtige/", include("den_hurtige.urls")),
    path("opslagstavle/", include("opslagstavle.urls")),
    path("begivenheder/", include("events.urls")),
    path("reparationer/", include("reparationer.urls")),
    path("arkiv/", include("arkiv.urls")),
    path("ak/", include("ak.urls")),
    path("oelkaelder/", include("oelkaelder.urls")),
    path("statistik/", include("stats.urls")),
    path("vaerelsestjek/", include("rooms.urls_vaerelsestjek")),
    path("soegvaerelse/", include("rooms.urls_soegvaerelse")),
    path(
        "admin/login",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            authentication_form=EmailAuthenticationForm,
        ),
        name="login",
    ),
    path("admin/logout", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "admin/password-change",
        auth_views.PasswordChangeView.as_view(
            template_name="registration/password_change.html",
            success_url="/intern/admin/password-change/done",
        ),
        name="password_change",
    ),
    path(
        "admin/password-change/done",
        auth_views.PasswordChangeDoneView.as_view(
            template_name="registration/password_change_done.html",
        ),
        name="password_change_done",
    ),
    # Password reset (F-014) — links expire after 2h (PASSWORD_RESET_TIMEOUT)
    path(
        "admin/password-reset",
        auth_views.PasswordResetView.as_view(
            form_class=ResidentPasswordResetForm,  # also reaches residents who never set a password
            template_name="registration/password_reset_form.html",
            email_template_name="registration/password_reset_email.html",
            subject_template_name="registration/password_reset_subject.txt",
            success_url="/intern/admin/password-reset/done",
        ),
        name="password_reset",
    ),
    path(
        "admin/password-reset/done",
        auth_views.PasswordResetDoneView.as_view(template_name="registration/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "admin/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html", success_url="/intern/admin/reset/done"
        ),
        name="password_reset_confirm",
    ),
    path(
        "admin/reset/done",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),
]
