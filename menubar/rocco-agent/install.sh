#!/usr/bin/env bash
# Install rocco-agent on a remote Ubuntu host (Rocco) as a systemd --user
# service. No root required on the target — uses `systemctl --user` and
# `loginctl enable-linger` so the agent survives logout.
#
# Usage:
#   bash menubar/rocco-agent/install.sh [hostname]
#
# Default hostname is `rocco` (which should be defined in your ~/.ssh/config
# with a ControlMaster entry — see menubar/README.md).
#
# Idempotent: re-running just refreshes the script + unit file and restarts
# the service.

set -euo pipefail

HOST="${1:-rocco}"
REMOTE_USER_DEFAULT="amastropaolo"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_PY="${HERE}/rocco-agent.py"
UNIT_FILE="${HERE}/rocco-agent.service"

if [[ ! -f "${AGENT_PY}" ]]; then
    echo "FATAL: ${AGENT_PY} not found" >&2
    exit 1
fi
if [[ ! -f "${UNIT_FILE}" ]]; then
    echo "FATAL: ${UNIT_FILE} not found" >&2
    exit 1
fi

# Resolve the remote login name (so enable-linger uses the right user).
REMOTE_USER="$(ssh -o BatchMode=yes "${HOST}" 'echo "$USER"' 2>/dev/null || true)"
if [[ -z "${REMOTE_USER}" ]]; then
    echo "Warning: could not resolve remote user via SSH, defaulting to ${REMOTE_USER_DEFAULT}" >&2
    REMOTE_USER="${REMOTE_USER_DEFAULT}"
fi

echo "==> Installing rocco-agent on ${HOST} (user=${REMOTE_USER})"

# 1. Create remote target dirs.
ssh -o BatchMode=yes "${HOST}" '
    set -euo pipefail
    mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user" "$HOME/.cache"
'

# 2. Copy the agent script + unit file.
echo "==> scp rocco-agent.py -> ${HOST}:~/.local/bin/"
scp -q -o BatchMode=yes "${AGENT_PY}" "${HOST}:~/.local/bin/rocco-agent.py"
ssh -o BatchMode=yes "${HOST}" 'chmod +x "$HOME/.local/bin/rocco-agent.py"'

echo "==> scp rocco-agent.service -> ${HOST}:~/.config/systemd/user/"
scp -q -o BatchMode=yes "${UNIT_FILE}" "${HOST}:~/.config/systemd/user/rocco-agent.service"

# 3. Enable lingering so the user manager runs even when nobody is logged in.
#    This DOES require sudo on the target (it's the one privileged step).
echo "==> loginctl enable-linger ${REMOTE_USER} (may prompt for sudo)"
ssh -t "${HOST}" "sudo loginctl enable-linger ${REMOTE_USER}" || {
    echo "Note: enable-linger failed — agent will only run while you have an" >&2
    echo "      active login session on ${HOST}. Re-run with sudo to fix." >&2
}

# 4. Daemon-reload + enable + start.
echo "==> systemctl --user daemon-reload && enable --now rocco-agent"
ssh -o BatchMode=yes "${HOST}" '
    set -euo pipefail
    systemctl --user daemon-reload
    systemctl --user enable --now rocco-agent.service
'

echo "==> Status:"
ssh -o BatchMode=yes "${HOST}" 'systemctl --user --no-pager status rocco-agent.service' || true

cat <<EOF

Done. To verify the snapshot file:
    ssh ${HOST} cat \\~/.cache/rocco-status.json | jq .

To tail logs:
    ssh ${HOST} journalctl --user -u rocco-agent -f
EOF
