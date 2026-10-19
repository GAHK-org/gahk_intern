#!/usr/bin/env bash
# Redeploys the app through Coolify when CI has moved ghcr.io/gahk-org/gahk_intern:main to a new
# image (DEPLOY.md §4a). Run by gahk-autodeploy.timer; configured by /etc/gahk-autodeploy.env.
#
# It runs on the server and only ever calls out to GHCR and in to the local Coolify, so GitHub holds
# no credential for the server: CI publishes, and this side decides to deploy.
set -euo pipefail

: "${COOLIFY_TOKEN:?set in /etc/gahk-autodeploy.env}"
: "${COOLIFY_APP_UUID:?set in /etc/gahk-autodeploy.env}"
IMAGE="${IMAGE:-ghcr.io/gahk-org/gahk_intern:main}"
COOLIFY_URL="${COOLIFY_URL:-http://localhost:8000}"
STATE_FILE="${STATE_FILE:-/var/lib/gahk-autodeploy/deployed-image}"

# Pulled to see whether :main has moved; a pull with nothing new is one manifest request to GHCR.
# The deploy checks again (`pull_policy: always` in docker-compose.prod.yml) and finds it up to date.
docker pull --quiet "$IMAGE" >/dev/null
current=$(docker image inspect --format '{{.Id}}' "$IMAGE")
deployed=$(cat "$STATE_FILE" 2>/dev/null || true)
if [ "$current" = "$deployed" ]; then
  exit 0
fi

revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE")
echo "New image ${current} (commit ${revision:-unknown}); asking Coolify to deploy"
curl --fail-with-body --silent --show-error --max-time 30 \
  --header "Authorization: Bearer ${COOLIFY_TOKEN}" \
  "${COOLIFY_URL}/api/v1/deploy?uuid=${COOLIFY_APP_UUID}"
echo

# Recorded once Coolify has accepted the request, so a failed call is retried on the next run. A
# deploy that Coolify accepts and that then fails (a broken migration, say) is NOT retried: doing it
# again every few minutes would not fix it. Fix forward, or redeploy from Coolify by hand.
mkdir -p "$(dirname "$STATE_FILE")"
printf '%s\n' "$current" > "$STATE_FILE"
