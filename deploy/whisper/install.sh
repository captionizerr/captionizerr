#!/bin/sh
# Install the Whisper service on a Debian 12/13 host or LXC (run as root, from this folder).
# Usage: sh install.sh [allowed client IP ...]   e.g. sh install.sh 192.0.2.10 192.0.2.20
# (the CaptionizeRR/Bazarr host, and your Prometheus if it scrapes /metrics)
set -eu
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv ffmpeg nftables >/dev/null
id whisper >/dev/null 2>&1 || useradd --system --home /opt/whisper --shell /usr/sbin/nologin whisper
install -d -o whisper -g whisper /opt/whisper
install -m 644 whisper_service.py /opt/whisper/whisper_service.py
[ -x /opt/whisper/venv/bin/python ] || python3 -m venv /opt/whisper/venv
/opt/whisper/venv/bin/pip install -q --upgrade pip
/opt/whisper/venv/bin/pip install -q "faster-whisper==1.*" "fastapi==0.*" "uvicorn==0.*" "python-multipart==0.*"
chown -R whisper:whisper /opt/whisper
install -m 644 whisper.service /etc/systemd/system/whisper.service
if [ "$#" -gt 0 ]; then   # only these clients may reach port 9000
  allow=$(printf '%s, ' "$@" | sed 's/, $//')
  cat > /etc/nftables.conf <<NFT
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority 0; policy accept;
    tcp dport 9000 ip saddr { 127.0.0.1, $allow } accept
    tcp dport 9000 drop
  }
}
NFT
  systemctl enable -q --now nftables && nft -f /etc/nftables.conf
fi
systemctl daemon-reload
systemctl enable -q --now whisper
echo "whisper: starting (the first start downloads the model)"
