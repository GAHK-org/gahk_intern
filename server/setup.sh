#!/usr/bin/env bash
# The Coolify server's own settings, which no container can make: swap, the kernel's use of it, and
# earlyoom. DEPLOY.md §4f says why. Safe to run again: each step checks what is already in place.
#
# On the running server, from a checkout:   ssh root@<server> 'bash -s' < server/setup.sh
# On a new server: server/cloud-init.yml runs it on first boot.
set -euo pipefail

SWAP_FILE=/swapfile
SWAP_SIZE=2G

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root." >&2
    exit 1
fi

# Swap, for Coolify, the databases and MediaWiki. The app's own containers cannot use it
# (memswap_limit in docker-compose.prod.yml), so a runaway job is still stopped at its limit.
if ! swapon --show=NAME --noheadings | grep -qx "$SWAP_FILE"; then
    if [ ! -f "$SWAP_FILE" ]; then
        fallocate -l "$SWAP_SIZE" "$SWAP_FILE"
        chmod 600 "$SWAP_FILE"
        mkswap "$SWAP_FILE" >/dev/null
    fi
    swapon "$SWAP_FILE"
    echo "Swap on: $SWAP_FILE ($SWAP_SIZE)"
fi
if ! grep -q "^$SWAP_FILE " /etc/fstab; then
    echo "$SWAP_FILE none swap sw 0 0" >>/etc/fstab
fi

# Swap only under pressure: the default (60) also moves idle memory out while there is room, and the
# databases then wait on the disk for pages they had in memory.
cat >/etc/sysctl.d/99-gahk.conf <<'EOF'
# Written by server/setup.sh in gahk_intern; edit it there.
vm.swappiness = 10
EOF
sysctl -q -p /etc/sysctl.d/99-gahk.conf

# earlyoom stops a process before the box runs out, while the kernel's own killer only acts once
# everything has stalled. It acts when free memory is under 5 % and free swap under 10 %, and then
# picks a worker or ffmpeg first and never a database, the proxy or Docker. The patterns match the
# process name, and have no spaces so that systemd's splitting of EARLYOOM_ARGS cannot cut them.
if ! command -v earlyoom >/dev/null; then
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q earlyoom
fi
cat >/etc/default/earlyoom <<'EOF'
# Written by server/setup.sh in gahk_intern; edit it there.
EARLYOOM_ARGS="-m 5 -s 10 -r 3600 --prefer ^(ffmpeg|celery)$ --avoid ^(postgres|mariadbd|mysqld|dockerd|containerd.*|traefik|redis-server|php-fpm.*|sshd|systemd.*)$"
EOF
systemctl enable -q earlyoom
systemctl restart earlyoom

echo "Done: $(swapon --show=NAME,SIZE --noheadings | tr -s ' ') swap, swappiness $(sysctl -n vm.swappiness), earlyoom $(systemctl is-active earlyoom)."
