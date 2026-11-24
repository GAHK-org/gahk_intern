"""The intranet + auth, at the root of intern.gahk.dk (config.urls). F-013/F-014."""

from django.contrib.auth import views as auth_views
from django.urls import include, path, reverse_lazy

from access.api import Feature, in_feature

from . import auth, views
from .forms import EmailAuthenticationForm, ResidentPasswordResetForm

urlpatterns = [
    path("", in_feature(Feature.DASHBOARD, views.dashboard), name="dashboard"),
    path("regnskab/", in_feature(Feature.ACCOUNTING, include("regnskab.urls"))),
    path("alumneliste/", in_feature(Feature.DIRECTORY, views.directory), name="directory"),
    path("alumneliste/rows", in_feature(Feature.DIRECTORY, views.directory_rows), name="directory_rows"),
    path(
        "alumneliste/eksport", in_feature(Feature.DIRECTORY, views.directory_export), name="directory_export"
    ),
    path(
        "alumneliste/naeste-maaned",
        in_feature(Feature.DIRECTORY, views.next_month_list),
        name="next_month_list",
    ),
    path("beboer/<int:pk>/rediger", in_feature(Feature.DIRECTORY, views.edit_resident), name="edit_resident"),
    path("beboer/<int:pk>/profil", in_feature(Feature.PROFILES, views.profile), name="resident_profile"),
    path("beboere/naevn", in_feature(Feature.MENTIONS, views.mention_lookup), name="mention_lookup"),
    path("beboer/min-profil/rediger", in_feature(Feature.PROFILES, views.edit_profile), name="edit_profile"),
    path("stamtree/", in_feature(Feature.STAMTREE, views.stamtree), name="stamtree"),
    path("notifikationer/", in_feature(Feature.NOTIFICATIONS, include("notifications.urls"))),
    path("den-hurtige/", in_feature(Feature.DEN_HURTIGE, include("den_hurtige.urls"))),
    path("opslagstavle/", in_feature(Feature.OPSLAGSTAVLE, include("opslagstavle.urls"))),
    path("begivenheder/", in_feature(Feature.EVENTS, include("events.urls"))),
    path("reparationer/", in_feature(Feature.REPARATIONER, include("reparationer.urls"))),
    path("fotoalbum/", in_feature(Feature.PHOTO_ALBUM, include("photo_album.urls"))),
    path("gahkflix/", in_feature(Feature.GAHKFLIX, include("gahkflix.urls"))),
    path("ak/", in_feature(Feature.AK, include("ak.urls"))),
    path("oelkaelder/", in_feature(Feature.OELKAELDER, include("oelkaelder.urls"))),
    path("credit/", in_feature(Feature.CREDIT, include("credit.urls"))),
    path("statistik/", in_feature(Feature.STATS, include("stats.urls"))),
    path("vaerelsestjek/", in_feature(Feature.VAERELSESTJEK, include("rooms.urls_vaerelsestjek"))),
    path("soegvaerelse/", in_feature(Feature.SOEGVAERELSE, include("rooms.urls_soegvaerelse"))),
]

# Logging in and out and the password pages: every account reaches them, and nobody logged in too.
urlpatterns += in_feature(
    Feature.ACCOUNT,
    [
        path(
            "admin/login",
            auth.LoginView.as_view(
                template_name="registration/login.html",
                authentication_form=EmailAuthenticationForm,
            ),
            name="login",
        ),
        path("admin/logout", auth_views.LogoutView.as_view(), name="logout"),
        path(
            "admin/password-change",
            auth.PasswordChangeView.as_view(
                template_name="registration/password_change.html",
                success_url=reverse_lazy("password_change_done"),
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
            auth.PasswordResetView.as_view(
                form_class=ResidentPasswordResetForm,  # also reaches residents who never set a password
                template_name="registration/password_reset_form.html",
                email_template_name="registration/password_reset_email.html",
                subject_template_name="registration/password_reset_subject.txt",
                success_url=reverse_lazy("password_reset_done"),
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
            auth.PasswordResetConfirmView.as_view(
                template_name="registration/password_reset_confirm.html",
                success_url=reverse_lazy("password_reset_complete"),
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
    ],
)
