#!/usr/bin/env bash
set -euo pipefail
umask 022

# This file is installed by the VPS operator and runs as framefable-deploy.
revision=${1:-}
if [[ ! $revision =~ ^[0-9a-f]{40}$ || $# -ne 1 ]]; then
    printf '%s\n' 'Expected one Git commit SHA.' >&2
    exit 2
fi

root=/opt/framefable
releases=$root/releases
port=4173 # Set this to the PORT in /etc/framefable.env.
cd "$releases"

exec 9>.deploy.lock
flock -x 9
previous=$(readlink current)
staged=$(mktemp -d "$releases/$revision.XXXXXX")
chmod 755 "$staged"

tar --extract --gzip --file=- --directory="$staged" --no-same-owner --no-same-permissions
ln -s "$root/.venv" "$staged/.venv"
ln -s "$root/.models" "$staged/.models"
if [[ ! -x $staged/run.sh || ! -f $staged/public/index.html ]]; then
    printf '%s\n' 'The release is missing its launcher or frontend.' >&2
    exit 1
fi
(cd "$staged" && .venv/bin/python -B -c 'import main')

activate() {
    ln -sfn "$1" current.next
    mv -Tf current.next current
}

healthy() {
    local attempt
    for ((attempt = 0; attempt < 15; attempt++)); do
        if curl -fsS --max-time 3 "http://127.0.0.1:$port/" | grep -F '<title>FrameFable</title>' >/dev/null; then
            return 0
        fi
        sleep 2
    done
    return 1
}

activate "$staged"
if sudo -n /usr/bin/systemctl restart framefable.service && healthy; then
    printf 'Deployed %s\n' "$revision"
else
    printf 'Release %s failed its health check. Restoring the previous release.\n' "$revision" >&2
    activate "$previous"
    if ! sudo -n /usr/bin/systemctl restart framefable.service || ! healthy; then
        printf '%s\n' 'The previous release did not come back. Check journalctl -u framefable.' >&2
    fi
    exit 1
fi
