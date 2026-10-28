#!/usr/bin/env bash
# LabBot 에이전트 설치 (각 GPU 서버와 NAS에서 실행)
# 사용법: sudo ./install-agent.sh <봇주소> <토큰> [서버이름] [디스크경로들]
#   예) sudo ./install-agent.sh http://192.168.0.10:8787/metrics abc123 gpu1 /,/home,/data
#   예) sudo ./install-agent.sh http://192.168.0.10:8787/metrics abc123 nas /volume1
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "sudo로 실행해 주세요."; exit 1; fi
if [[ $# -lt 2 ]]; then sed -n '2,5p' "$0"; exit 1; fi

URL="$1"; TOKEN="$2"; NAME="${3:-$(hostname -s)}"; DISKS="${4:-/}"
DIR="$(cd "$(dirname "$0")" && pwd)"

command -v python3 >/dev/null || { echo "python3가 필요해요."; exit 1; }

install -d /opt/labbot-agent
install -m 755 "$DIR/labbot_agent.py" /opt/labbot-agent/labbot_agent.py

umask 077
cat > /etc/labbot-agent.env <<EOF
LABBOT_URL=$URL
LABBOT_TOKEN=$TOKEN
LABBOT_HOST=$NAME
LABBOT_DISKS=$DISKS
LABBOT_INTERVAL=60
EOF

install -m 644 "$DIR/labbot-agent.service" /etc/systemd/system/labbot-agent.service
systemctl daemon-reload
systemctl enable --now labbot-agent

echo "── 수집 테스트 ──"
set -a; . /etc/labbot-agent.env; set +a
python3 /opt/labbot-agent/labbot_agent.py --once --print | head -40
echo
echo "설치 완료: systemctl status labbot-agent / journalctl -u labbot-agent -f"
