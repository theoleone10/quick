#!/usr/bin/env bash
# Ship the code to the server and restart.   usage: deploy/push.sh root@SERVER_IP
set -euo pipefail
HOST=${1:?usage: deploy/push.sh root@SERVER_IP}
cd "$(dirname "$0")/.."
tar czf - server.py quick.py AGENT_GUIDE.md client \
  | ssh -i ~/.ssh/quick_ed25519 "$HOST" '
      tar xzf - -C /opt/quick && chown -R root:root /opt/quick &&
      systemctl restart quick && sleep 2 &&
      systemctl is-active quick && journalctl -u quick -n 3 --no-pager -o cat'
