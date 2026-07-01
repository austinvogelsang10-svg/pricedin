#!/usr/bin/env bash
# Priced In — one-shot droplet setup. Run as root from /opt/pricedin.
set -e
cd "$(dirname "$0")"

echo "==> Installing system packages"
apt-get update -y >/dev/null
apt-get install -y python3-venv git >/dev/null

echo "==> Python virtualenv + dependencies"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

[ -f .env ] || { cp .env.example .env; echo "==> Created .env (fill it in next)"; }

# 1GB swap on small droplets (no-op if swap exists or RAM >= 1.5GB)
if [ "$(free -m | awk '/Mem:/{print $2}')" -lt 1500 ] && ! swapon --show | grep -q .; then
  echo "==> Adding 1GB swapfile"
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Installing systemd timers"
cp systemd/pricedin-hourly.service systemd/pricedin-hourly.timer \
   systemd/pricedin-daily.service  systemd/pricedin-daily.timer  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pricedin-hourly.timer pricedin-daily.timer

echo ""
echo "Setup complete. Next:"
echo "  1) nano .env            (paste your keys, Ctrl+O Enter Ctrl+X to save)"
echo "  2) systemctl start pricedin-hourly    (first live run)"
echo "  3) journalctl -u pricedin-hourly -n 40 --no-pager"
