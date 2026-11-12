#!/usr/bin/env bash
# One-time server setup for a fresh Ubuntu 22.04/24.04 VPS. Run as root.
# Expects /tmp/quick-setup/ to contain: quick.service quick.env origin.pem origin.key
set -euo pipefail
SRC=/tmp/quick-setup

# 1 GB swap: small droplets run out of memory during pip installs otherwise.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

export DEBIAN_FRONTEND=noninteractive
# Fresh droplets run updates on first boot; wait for them.
while pgrep -x "apt-get|apt|dpkg" >/dev/null; do sleep 5; done
apt-get -o DPkg::Lock::Timeout=600 update -qq
apt-get -o DPkg::Lock::Timeout=600 install -y -qq python3 python3-venv ufw curl sqlite3 unattended-upgrades >/dev/null

id quick >/dev/null 2>&1 || useradd --system --home /var/lib/quick --shell /usr/sbin/nologin quick
mkdir -p /opt/quick /var/lib/quick /etc/quick
chown quick:quick /var/lib/quick
chmod 750 /var/lib/quick

[ -x /opt/quick/venv/bin/python ] || python3 -m venv /opt/quick/venv
/opt/quick/venv/bin/pip install -q --upgrade pip anthropic

# Secrets + Cloudflare origin certificate
[ -f "$SRC/quick.env" ]  && install -m 600 -o root -g root  "$SRC/quick.env"  /etc/quick/quick.env
[ -f "$SRC/origin.pem" ] && install -m 644 -o root -g quick "$SRC/origin.pem" /etc/quick/origin.pem
[ -f "$SRC/origin.key" ] && install -m 640 -o root -g quick "$SRC/origin.key" /etc/quick/origin.key
install -m 644 "$SRC/quick.service" /etc/systemd/system/quick.service


# Firewall: SSH from anywhere, HTTPS only from Cloudflare (so nobody can bypass it).
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
for ip in $(curl -fsS https://www.cloudflare.com/ips-v4) $(curl -fsS https://www.cloudflare.com/ips-v6); do
  ufw allow proto tcp from "$ip" to any port 443 >/dev/null
done
ufw --force enable >/dev/null

# Nightly database backup, keep 14 days.
cat > /etc/cron.daily/quick-backup <<'EOF'
#!/bin/sh
[ -f /var/lib/quick/quick.db ] || exit 0
install -d -o quick -g quick -m 750 /var/backups/quick
# Run as quick so SQLite never leaves root-owned files in the data dir.
runuser -u quick -- sqlite3 /var/lib/quick/quick.db ".backup /var/backups/quick/quick-$(date +%F).db"
tar czf /var/backups/quick/sites-$(date +%F).tgz -C /var/lib/quick sites files 2>/dev/null
find /var/backups/quick -mtime +14 -delete
EOF
chmod 755 /etc/cron.daily/quick-backup

systemctl daemon-reload
systemctl enable quick >/dev/null
rm -rf "$SRC"
echo "setup done"
