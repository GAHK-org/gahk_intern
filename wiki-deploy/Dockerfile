# GAHK wiki — MediaWiki 1.35 on the official image, plus the parts that are actually ours.
#
# Why not ship legacy_site/wiki/ wholesale: that tree has been upgraded in place since at least 1.22.
# RELEASE-NOTES-1.22 is still there; so is includes/AuthPlugin.php, which MediaWiki removed in 1.33;
# so is includes/installer/Installer.i18n.php, which is absent from the official 1.35 image entirely.
# Publishing that tree would publish every dead file with it. The official image gives patched 1.35.x
# core, and only these 8 directories plus LocalSettings.php are genuinely ours — verified with
# `comm` against the image's own extension/skin lists (CUTOVER.md Phase 5A step 3).
#
#     docker build -t gahk-wiki:1.35 .
#
# PHP comes from the base image and is currently 8.0.30 — 1.35's composer.json only requires
# ">=7.3.19", and the official image maintainers ship 8.0. Do not assume 7.4 here. The GahkAuthLogin
# sources are `php -l` clean on 7.4, 8.0 and 8.1, so they carry through the eventual 1.43 upgrade
# (which requires 8.1+) unchanged.

FROM mediawiki:1.35

# pdo_pgsql: GahkAuthLogin authenticates against the Django app's PostgreSQL (residents_resident)
# instead of a frozen copy of the retired intern_alumne table, so a password reset in the app takes
# effect on the wiki immediately rather than leaving the old hashes valid here forever.
#
# Do NOT `apt-get purge --auto-remove libpq-dev` afterwards: libpq5 is pulled in as its dependency,
# auto-remove takes libpq5 with it, and pdo_pgsql.so is then unloadable. PHP skips an extension it
# cannot load *silently*, so the build still succeeds and the breakage only appears as a confusing
# failure at the first login. Keeping libpq-dev costs a few MB. The assertion below is what actually
# guarantees it — never trust the build exiting 0.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends libpq-dev; \
    docker-php-ext-install -j"$(nproc)" pdo_pgsql; \
    rm -rf /var/lib/apt/lists/*; \
    php -m | grep -qx 'pdo_pgsql' || { echo 'FATAL: pdo_pgsql did not load'; exit 1; }; \
    php -m | grep -qx 'mysqli'    || { echo 'FATAL: mysqli missing (MediaWiki core needs it)'; exit 1; }

# Extensions and skins NOT bundled with the 1.35 tarball. Everything else your old tree carried is
# bundled, and the image's copies are newer — do not add bundled ones here or you undo its patches.
COPY extensions/ /var/www/html/extensions/
COPY skins/      /var/www/html/skins/

COPY LocalSettings.php /var/www/html/LocalSettings.php

# $wgScriptPath is "/wiki" (the legacy URL, preserved per DEPLOY.md §7.3) but the image serves
# MediaWiki from the document root. Whether the reverse proxy strips the /wiki prefix decides which
# of /index.php or /wiki/index.php actually reaches Apache — so serve both. With this alias the
# container is correct either way, instead of depending on a proxy behaviour we would otherwise have
# to discover by trial and error in production.
RUN printf '%s\n' \
      'Alias /wiki /var/www/html' \
      '<Directory /var/www/html>' \
      '    Require all granted' \
      '    AllowOverride All' \
      '</Directory>' \
      > /etc/apache2/conf-available/gahk-wiki-alias.conf \
    && a2enconf gahk-wiki-alias \
    && a2enmod rewrite

# PHP's upload limits, which the official image leaves at the stock 2M/8M. That is below a single
# phone photograph, and the way it fails is the problem: PHP over upload_max_filesize discards the
# FILE and MediaWiki reports "the file you uploaded seems to be empty", while over post_max_size it
# discards the whole POST body including the CSRF token and MediaWiki reports a session failure.
# Neither message mentions a size, nothing is logged, and the wiki is otherwise perfectly healthy —
# so it presents as "uploads are broken" with no thread to pull.
#
# THE ORDER OF THE THREE MATTERS, and it is why MediaWiki's own $wgMaxUploadSize (LocalSettings.php)
# is set BELOW upload_max_filesize rather than equal to it: MediaWiki checks the size after PHP has
# already accepted the file, so it can only produce a clear error for uploads PHP let through. Leave
# a margin and MediaWiki always speaks first. post_max_size must exceed upload_max_filesize because
# the description and tokens travel with the file, and memory_limit must exceed post_max_size or
# thumbnailing a large JPEG dies *after* a successful upload — which looks like a different bug.
RUN set -eux; \
    printf '%s\n' \
      'upload_max_filesize = 72M' \
      'post_max_size = 80M' \
      'memory_limit = 256M' \
      > /usr/local/etc/php/conf.d/gahk-uploads.ini; \
    php -i | grep -qx 'upload_max_filesize => 72M => 72M' \
      || { echo 'FATAL: PHP did not pick up gahk-uploads.ini'; exit 1; }

# User uploads (396 rows in wikiimage). Must be a mounted volume or they vanish on every redeploy.
VOLUME /var/www/html/images

# Ownership of the uploads volume, asserted at every start. See entrypoint.sh for why it cannot be a
# RUN chown here: a populated named volume shadows whatever the image put at that path, so the only
# moment this can be fixed is after the mount exists.
#
# CMD is repeated deliberately. Setting ENTRYPOINT in a derived image resets the CMD inherited from
# the base, so omitting it would leave the container with nothing to run.
COPY entrypoint.sh /usr/local/bin/gahk-entrypoint.sh
RUN chmod +x /usr/local/bin/gahk-entrypoint.sh
ENTRYPOINT ["gahk-entrypoint.sh"]
CMD ["apache2-foreground"]
