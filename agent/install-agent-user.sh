#!/usr/bin/env bash
# LabBot 에이전트 설치 (sudo 없이, 내 계정으로 실행)
# 사용법: bash ./install-agent-user.sh <봇주소> <토큰> [서버이름] [디스크경로들] [JOBS_TOKEN]
#   예) bash ./install-agent-user.sh http://100.x.y.z:8787/metrics abc123 gpu1 /,/home,/data def456
# JOBS_TOKEN을 주면 학습 작업 알림 CLI(lb)를 ~/.local/bin/lb 로 설치합니다 (내 계정에서만 사용).
# systemd 사용자 서비스로 등록하고, 로그아웃해도 계속 돌도록 linger를 켭니다.
# 그게 안 되는 서버에서는 crontab(@reboot)으로 대신 등록합니다.
set -euo pipefail

if [[ $# -lt 2 ]]; then sed -n '2,7p' "$0"; exit 1; fi

URL="$1"; TOKEN="$2"; NAME="${3:-$(hostname -s)}"; DISKS="${4:-/}"; JOBS_TOKEN="${5:-}"
SRC="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.labbot-agent"

command -v python3 >/dev/null || { echo "python3가 필요해요."; exit 1; }

mkdir -p "$DIR"
install -m 755 "$SRC/labbot_agent.py" "$DIR/labbot_agent.py"

umask 077
cat > "$DIR/agent.env" <<EOF
LABBOT_URL=$URL
LABBOT_TOKEN=$TOKEN
LABBOT_HOST=$NAME
LABBOT_DISKS=$DISKS
LABBOT_INTERVAL=60
EOF

# systemd/cron 어디서 실행해도 같은 설정을 읽도록 실행 스크립트를 둡니다.
cat > "$DIR/run.sh" <<'EOF'
#!/usr/bin/env bash
set -a; . "$HOME/.labbot-agent/agent.env"; set +a
exec nice -n 10 python3 "$HOME/.labbot-agent/labbot_agent.py"
EOF
chmod 700 "$DIR/run.sh"

if [[ -n "$JOBS_TOKEN" ]]; then
    mkdir -p "$HOME/.local/bin" "$HOME/.config"
    install -m 755 "$SRC/lb" "$HOME/.local/bin/lb"
    cat > "$HOME/.config/labbot-lb.conf" <<EOF
LABBOT_JOBS_URL=${URL%/metrics}/jobs
LABBOT_JOBS_TOKEN=$JOBS_TOKEN
LABBOT_HOST=$NAME
EOF
    echo "lb 설치: ~/.local/bin/lb (설정 ~/.config/labbot-lb.conf)"
    # ~/.local/bin 이 PATH에 없으면 셸 설정에 추가 (Ubuntu는 로그인 시 폴더가 있어야만 PATH에 넣어 줌)
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *)
            line='export PATH="$HOME/.local/bin:$PATH"  # labbot lb'
            for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
                if [[ -f "$rc" ]] && ! grep -qF "# labbot lb" "$rc"; then
                    echo "$line" >> "$rc"
                    echo "PATH에 ~/.local/bin 추가: $rc"
                fi
            done
            echo "⚠️ 지금 셸에서 바로 쓰려면: export PATH=\"\$HOME/.local/bin:\$PATH\"  (또는 다시 로그인)"
            ;;
    esac
elif [[ ! -f "$HOME/.local/bin/lb" ]]; then
    echo "ℹ️ JOBS_TOKEN을 주지 않아 lb(학습 작업 알림)는 설치하지 않았어요 (다섯 번째 인자)."
fi

echo "── 수집 테스트 ──"
set -a; . "$DIR/agent.env"; set +a
python3 "$DIR/labbot_agent.py" --once --print | head -40
echo

mode=""
if systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/labbot-agent.service" <<EOF
[Unit]
Description=LabBot server agent (GPU/CPU/disk metrics)

[Service]
ExecStart=$DIR/run.sh
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable labbot-agent
    systemctl --user restart labbot-agent
    if loginctl enable-linger "$USER" 2>/dev/null || [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" == "yes" ]]; then
        mode="systemd"
    else
        echo "⚠️ linger를 켜지 못했어요. 로그아웃하면 서비스가 멈출 수 있어서 crontab도 함께 등록해요."
        systemctl --user disable --now labbot-agent || true
    fi
fi

if [[ -z "$mode" ]]; then
    # crontab: 재부팅 시 시작 + 5분마다 살아 있는지 확인
    line_boot="@reboot $DIR/run.sh >> $DIR/agent.log 2>&1"
    line_check="*/5 * * * * pgrep -u $USER -f labbot-agent/labbot_agent.py >/dev/null || $DIR/run.sh >> $DIR/agent.log 2>&1"
    { crontab -l 2>/dev/null | grep -v "labbot-agent" || true; echo "$line_boot"; echo "$line_check"; } | crontab -
    pkill -u "$USER" -f labbot-agent/labbot_agent.py 2>/dev/null || true
    nohup "$DIR/run.sh" >> "$DIR/agent.log" 2>&1 &
    mode="cron"
fi

echo
if [[ "$mode" == "systemd" ]]; then
    echo "설치 완료 (systemd 사용자 서비스): systemctl --user status labbot-agent / journalctl --user -u labbot-agent -f"
else
    echo "설치 완료 (crontab): tail -f $DIR/agent.log / 멈추기: crontab -e 에서 labbot-agent 줄 삭제 후 pkill -f labbot_agent.py"
fi
