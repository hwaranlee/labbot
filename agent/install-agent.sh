#!/usr/bin/env bash
# LabBot 에이전트 설치 (각 GPU 서버와 NAS에서 실행)
# 사용법: sudo ./install-agent.sh <봇주소> <토큰> [서버이름] [디스크경로들] [JOBS_TOKEN]
#   예) sudo ./install-agent.sh http://192.168.0.10:8787/metrics abc123 gpu1 /,/home,/data def456
#   예) sudo ./install-agent.sh http://192.168.0.10:8787/metrics abc123 nas /volume1
# JOBS_TOKEN을 주면 학습 작업 알림 CLI(lb)를 /usr/local/bin/lb 로 설치하고 /etc/labbot-lb.conf 를 만듭니다.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "sudo로 실행해 주세요."; exit 1; fi
if [[ $# -lt 2 ]]; then sed -n '2,6p' "$0"; exit 1; fi

URL="$1"; TOKEN="$2"; NAME="${3:-$(hostname -s)}"; DISKS="${4:-/}"; JOBS_TOKEN="${5:-}"
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

# 학습 작업 알림 CLI: 일반 사용자가 실행하므로 설정 파일은 0644 (METRICS_TOKEN이 아닌 JOBS_TOKEN만 담음)
install -m 755 "$DIR/lb" /usr/local/bin/lb
if [[ -n "$JOBS_TOKEN" ]]; then
    cat > /etc/labbot-lb.conf <<EOF
LABBOT_JOBS_URL=${URL%/metrics}/jobs
LABBOT_JOBS_TOKEN=$JOBS_TOKEN
LABBOT_HOST=$NAME
EOF
    chmod 644 /etc/labbot-lb.conf
    echo "lb 설치: /usr/local/bin/lb (설정 /etc/labbot-lb.conf)"
elif [[ ! -f /etc/labbot-lb.conf ]]; then
    echo "ℹ️ JOBS_TOKEN을 주지 않아 lb 알림 설정은 건너뛰었어요 (다섯 번째 인자)."
fi

install -m 644 "$DIR/labbot-agent.service" /etc/systemd/system/labbot-agent.service
systemctl daemon-reload
systemctl enable labbot-agent
systemctl restart labbot-agent  # 재설치할 때 새 설정으로 다시 시작

echo "── 수집 테스트 ──"
set -a; . /etc/labbot-agent.env; set +a
python3 /opt/labbot-agent/labbot_agent.py --once --print | head -40
echo
echo "설치 완료: systemctl status labbot-agent / journalctl -u labbot-agent -f"
