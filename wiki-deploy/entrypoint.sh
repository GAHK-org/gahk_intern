#!/bin/sh
# Make the uploads directory writable by Apache before MediaWiki ever sees it.
#
# THE BUG THIS EXISTS FOR: MediaWiki's default fsLockManager writes to $wgUploadDirectory/lockdir,
# and it creates that directory on first use owned by whoever ran the code. Anything executed in
# this container AS ROOT - a `docker exec php maintenance/update.php`, say - therefore leaves a
# root-owned lockdir behind. Apache runs as www-data, so from that moment every upload fails with
# "Kunne ikke aabne laasefilen for mwstore://local-backend/...", while the wiki is otherwise
# perfectly healthy: pages load, images already stored still serve, nothing is logged, and disk and
# permissions on images/ itself all look fine. It happened on 2026-08-30 and cost a week.
#
# WHY THIS CANNOT BE A DOCKERFILE `RUN chown`: /var/www/html/images is a named volume. Docker copies
# the image's directory into a volume only when that volume is EMPTY; ours has been populated since
# the 4 August import, so whatever ownership the image sets is shadowed and ignored at runtime. The
# fix has to run when the container starts, against the mounted volume.
#
# Targeted rather than a blanket `chown -R`: on a healthy container find matches nothing and this
# costs one directory walk, and when it does fix something it says so - so a recurrence shows up in
# the deploy log instead of as a mystery a week later.
set -e

IMAGES=/var/www/html/images

if [ -d "$IMAGES" ] && [ -n "$(find "$IMAGES" ! -user www-data -print -quit 2>/dev/null)" ]; then
    echo "gahk-entrypoint: found paths under $IMAGES not owned by www-data; fixing" >&2
    # NOT fatal if it fails, despite `set -e` above. A container that refuses to boot is worse than
    # one whose uploads are broken: the wiki still serves every page and every image already stored,
    # and the line below is what tells somebody why uploading stopped working. Failing hard here
    # would turn a degraded wiki into no wiki.
    find "$IMAGES" ! -user www-data -exec chown www-data:www-data {} + \
        || echo "gahk-entrypoint: WARNING could not fix ownership; uploads will fail" >&2
fi

# Hand back to the base image's own entrypoint, which does the PHP setup this must not skip.
exec docker-php-entrypoint "$@"
