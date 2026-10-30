# 🧪 LabBot

연구실용 Slack 앱입니다. 할 일 관리, 출퇴근·휴가 관리, GPU 서버 모니터링, 미팅 action items 정리, 논문 탐색·요약을 하나의 앱에서 처리합니다.

```
                        Slack (Socket Mode: 봇이 Slack으로 나가는 연결만 사용)
                                   ▲
                                   │ wss://
┌──────────────────────────────────┴───────────────────┐
│ NAS  ─ LabBot 본체 (Python, SQLite)   :8787 지표 수신 │
│      └ 에이전트 (디스크 사용량)                       │
└──────────────▲───────────────────────▲───────────────┘
               │ HTTP POST (1분마다)   │
        ┌──────┴──────┐         ┌──────┴──────┐
        │ GPU 서버 1  │         │ GPU 서버 2  │
        │  에이전트   │         │  에이전트   │
        └─────────────┘         └─────────────┘
```

* **별도 서버는 필요 없습니다.** 봇 본체는 항상 켜져 있는 **NAS**에 두는 것을 권장합니다. GPU 서버는 재부팅이 잦고 부하가 커서 덜 적합합니다.
* Socket Mode라서 **공인 IP, 도메인, HTTPS 인증서가 필요 없습니다.** 봇이 있는 서버에서 인터넷으로 나가는 연결만 되면 됩니다.
* 에이전트는 **파이썬 표준 라이브러리만** 사용합니다. 서버에 따로 설치할 패키지가 없습니다.
* Prometheus는 필요 없습니다.
* 미팅 정리와 논문 요약에는 **Claude API**를 사용합니다. API 키가 없어도 동작하지만, 그때는 규칙 기반으로 정리하고 논문은 영어 초록 앞부분을 보여 줍니다.

---

## 1. Slack 앱 만들기 (5분)

1. https://api.slack.com/apps → **Create New App** → **From a manifest** → 워크스페이스 선택
2. 이 폴더의 `manifest.yaml` 내용을 붙여 넣고 → **Create**
3. **Basic Information → App-Level Tokens → Generate Token**
   이름은 아무거나, 권한은 `connections:write` → 만들어진 `xapp-...` 토큰을 복사 → `SLACK_APP_TOKEN`
4. **Install App → Install to Workspace** → `xoxb-...` 토큰 복사 → `SLACK_BOT_TOKEN`
5. Slack에서 채널 두 개를 만들고 봇을 초대합니다 (`/invite @labbot`).
   * `#출퇴근`: 출퇴근·휴가 알림이 올라갑니다. **이 채널의 멤버가 근태 관리 대상**이 됩니다.
   * `#서버알림`: 디스크, GPU, 서버 다운 알림과 일일 요약이 올라갑니다.
   * `#논문`: 매일·매주 논문 다이제스트가 올라갑니다.
   * `#미팅` (선택): 공유를 선택한 미팅 정리와 금요일 연구실 전체 action items 현황이 올라갑니다.
   * 연구실 채널 (선택): 매주 월요일 학회 마감 요약이 올라갑니다 (`DEADLINE_CHANNEL`).
6. ID 확인 방법
   * 채널 ID: 채널 이름 클릭 → 맨 아래 `C0...`
   * 사람 ID: 프로필 → `⋮` → **멤버 ID 복사** (`U0...`)

> **이미 앱을 만들었다면** 새 `manifest.yaml`로 앱 설정의 **App Manifest**를 덮어쓰고 저장한 뒤, **Install App → Reinstall** 해 주세요. `/deadline`·`/meeting`·`/papers` 명령과 스레드를 읽는 권한(`channels:history`)이 추가됐습니다.

### Claude API 키 (미팅 정리·논문 요약용)

1. https://platform.claude.com 에 로그인 → **API Keys** → 키 발급
2. `.env`의 `ANTHROPIC_API_KEY`에 넣습니다. 모델은 `LLM_MODEL`로 바꿀 수 있고, 기본값은 `claude-sonnet-5-5`입니다.
3. 비용 감각: 매일 논문 다이제스트 1회(초록 40편 비교)가 약 4~5센트, 미팅 정리 1건이 약 2센트 정도입니다. 연구실 규모라면 한 달에 몇 달러 수준입니다. (요금표: https://platform.claude.com/docs/en/about-claude/pricing)

## 2. 봇 본체 설치 (NAS)

### 방법 A: Docker (NAS에 Docker가 있으면 가장 간단)

```bash
cd labbot
cp .env.example .env      # 토큰·채널·사람 ID 입력
openssl rand -hex 24      # 이 값을 METRICS_TOKEN에 넣기
openssl rand -hex 24      # 한 번 더 만들어서 JOBS_TOKEN에 넣기 (학습 작업 알림용, 다른 값이어야 함)
docker compose up -d --build
docker compose logs -f    # "LabBot 시작"이 보이면 성공
```

### 방법 B: systemd + Python 3.10 이상

```bash
sudo useradd -r -m -d /opt/labbot labbot
sudo cp -r labbot/. /opt/labbot/ && sudo chown -R labbot: /opt/labbot && cd /opt/labbot
sudo -u labbot python3 -m venv .venv
sudo -u labbot .venv/bin/pip install -r requirements.txt
sudo -u labbot cp .env.example .env && sudo -u labbot nano .env
sudo cp deploy/labbot.service /etc/systemd/system/
sudo systemctl enable --now labbot
journalctl -u labbot -f
```

확인: `curl http://<NAS-IP>:8787/health` → `{"ok": true, ...}`

> 방화벽을 쓰고 있다면 GPU 서버에서 NAS의 8787 포트로 접근할 수 있게 열어 주세요. 연구실 내부망에서만 열어 두는 것을 권장합니다.

## 3. 에이전트 설치 (GPU 서버 2대 + NAS)

`agent/` 폴더를 각 서버에 복사한 뒤 실행합니다.

```bash
# GPU 서버 1 (마지막 인자 JOBS_TOKEN을 주면 학습 작업 알림 CLI `lb`도 설치)
sudo ./install-agent.sh http://<NAS-IP>:8787/metrics <METRICS_TOKEN> gpu1 /,/home,/data <JOBS_TOKEN>
# GPU 서버 2
sudo ./install-agent.sh http://<NAS-IP>:8787/metrics <METRICS_TOKEN> gpu2 /,/home,/data <JOBS_TOKEN>
# NAS (봇과 같은 서버여도 따로 설치. 주소는 127.0.0.1로)
sudo ./install-agent.sh http://127.0.0.1:8787/metrics <METRICS_TOKEN> nas /volume1
```

* **sudo 권한이 없는 서버**에서는 같은 인자로 `bash ./install-agent-user.sh ...`를 쓰세요. 내 계정의 systemd 사용자 서비스(안 되면 crontab)로 등록하고, root 없이도 모든 사용자의 GPU 프로세스를 볼 수 있습니다.
* 네 번째 인자는 감시할 디스크 경로이고, 서버마다 실제 마운트 경로로 바꿔 주세요 (`df -h`로 확인).
* 다섯 번째 인자 `JOBS_TOKEN`은 **선택**입니다. 봇 `.env`의 `JOBS_TOKEN`과 **같은 값**을 넣어야 하고, 학습 작업 알림(`lb`)을 쓸 GPU 서버에만 넣으면 됩니다.

  | | `JOBS_TOKEN` 넣음 | 안 넣음 |
  |---|---|---|
  | 에이전트 (`/gpu`, `/server`) | ✅ | ✅ (`METRICS_TOKEN`만 사용) |
  | `install-agent.sh` (sudo) | `/usr/local/bin/lb` + `/etc/labbot-lb.conf` 생성 | `lb` 파일만 설치, 설정 파일 없음 → 실행해도 알림이 가지 않음 |
  | `install-agent-user.sh` (sudo 없음) | `~/.local/bin/lb` + `~/.config/labbot-lb.conf` 생성 | `lb`를 설치하지 않음 |

  NAS처럼 모니터링만 할 서버는 빼도 됩니다. 나중에 `lb`가 필요해지면 같은 명령에 다섯 번째 인자를 붙여 다시 실행하세요.
* 설치가 끝나면 수집 결과가 한 번 출력됩니다. 1~2분 뒤 Slack에서 `/gpu`를 입력해 확인하세요.
* GPU 프로세스의 사용자가 `?`로 보이면 `/etc/systemd/system/labbot-agent.service`에서 `User=nobody` 줄을 지우고 `sudo systemctl daemon-reload && sudo systemctl restart labbot-agent`를 실행하세요.
* Docker 컨테이너 안에서 돌리는 작업은 컨테이너를 실행한 계정(대개 `root`)으로 표시됩니다.

### Google Sheet 연동 (선택)

주간 리포트를 보낼 때 같은 내용을 Google Sheet에 행으로 쌓아 둡니다. 봇 DB는 `RETENTION_DAYS`(기본 60일)가 지나면 원본 지표를 지우므로, 장기 통계는 시트에서 보면 됩니다.

1. [Google Cloud Console](https://console.cloud.google.com)에서 프로젝트를 만들고 **Google Sheets API**를 켭니다.
2. **IAM 및 관리자 → 서비스 계정**을 만들고 **키 → JSON**을 내려받습니다. NAS의 `data/google-sa.json`에 둡니다.
3. 빈 스프레드시트를 만들고, **공유**에서 서비스 계정 이메일(`...@...iam.gserviceaccount.com`)을 **편집자**로 추가합니다.
4. `.env`에 `GSHEET_ID`(URL의 `/d/<ID>/edit` 부분)와 `GOOGLE_SERVICE_ACCOUNT_FILE=/data/google-sa.json`(Docker 기준)을 넣고 재시작합니다.
5. 관리자가 `/gpu week sheet`로 바로 기록해 보면 설정을 확인할 수 있습니다.
   * `... Sheets API has not been used in project ... or it is disabled` → 서비스 계정이 속한 프로젝트에서 Google Sheets API를 켜고 몇 분 뒤 다시 시도하세요.
   * `PermissionError`·`SpreadsheetNotFound`만 나오면 → 시트를 서비스 계정 이메일과 **편집자**로 공유했는지 확인하세요.

탭은 자동으로 만들어집니다: `GPU 주간`(서버·GPU별), `사용자 주간`(리눅스 계정별), `예약 내역`(예약 1건당 1행, 예약 시간 동안의 사용률·메모리·사용 비율).

#### 출퇴근·휴가 시트 (별도 시트)

개인 정보라서 GPU 시트와 **다른 스프레드시트**를 씁니다. 공유 범위도 따로 관리하세요(예: 교수님·행정 담당만).

1. 빈 스프레드시트를 하나 더 만들고, 같은 서비스 계정 이메일을 **편집자**로 공유합니다.
2. `.env`에 `ATTENDANCE_GSHEET_ID=<시트 ID>`를 넣습니다. 서비스 계정 키(`GOOGLE_SERVICE_ACCOUNT_FILE`)는 같은 것을 씁니다.
3. 관리자가 `/attendance sheet`를 입력해 바로 확인합니다.

* 매일 04:30(`ATTENDANCE_SHEET_SYNC`)에 **이번 달·지난달 `출퇴근 YYYY-MM` 탭**과 **올해 `휴가 YYYY` 탭**(1월에는 작년 탭도)을 **통째로 다시 씁니다.**
* 나중에 고친 출퇴근 시각, 휴가 취소·반려도 다음 날 그대로 반영되고, 행이 중복되지 않습니다.
* 그래서 시트에서 직접 고친 내용은 다음 동기화 때 덮어써집니다. 메모가 필요하면 별도 탭이나 오른쪽 빈 열이 아닌 **다른 탭**에 적어 두세요.
* 원본은 계속 봇의 `labbot.db`에 있고, 시트는 보기용 사본입니다.

## 4. 처음 쓸 때 할 일

* 논문 주제를 등록하세요. 예: `/papers add video diffusion, video generation cat:cs.CV` 다음 `/papers profile 우리 연구실은 ...` (연구실 소개 2~3문장, 관련도 판단에 사용)
* 정기 개인 미팅이 있으면 등록하세요. 예: `/meeting recurring @김철수 tue 14:00 60`
* 학생마다 한 번씩 `/gpu link <linux-user>`를 입력해 달라고 안내하세요. 계정을 연결해야 GPU를 잡아 두고 쓰지 않을 때 **본인에게 DM**이 가고, 사용 통계에 이름이 표시됩니다.
* 관리자는 `/gpu link kim @김철수`처럼 다른 사람의 계정도 대신 연결할 수 있습니다.

## 5. 운영: 끄기 · 켜기 · 업데이트 · DB 초기화

| 하려는 일 | Docker (방법 A, `labbot` 폴더에서) | systemd (방법 B) |
|---|---|---|
| 봇 끄기 | `docker compose down` | `sudo systemctl stop labbot` |
| 봇 켜기 | `docker compose up -d` | `sudo systemctl start labbot` |
| `.env`만 바꾼 뒤 반영 | `docker compose up -d` (`restart`는 `.env`를 다시 읽지 않음) | `sudo systemctl restart labbot` |
| 코드 업데이트 후 반영 | `git pull && docker compose up -d --build` | `cd /opt/labbot && sudo -u labbot git pull && sudo -u labbot .venv/bin/pip install -r requirements.txt && sudo systemctl restart labbot` |
| 로그 보기 | `docker compose logs -f` | `journalctl -u labbot -f` |

* 켠 뒤 로그에 `LabBot 시작`이 보이고 `curl http://<NAS-IP>:8787/health`가 `{"ok": true, ...}`이면 정상입니다.
* `manifest.yaml`이 바뀐 업데이트(새 슬래시 명령 등)는 Slack 앱 설정 → **App Manifest**에 붙여 넣고 저장한 뒤, 안내가 나오면 앱을 다시 설치해야 명령이 보입니다.
* 에이전트(`agent/`)가 바뀐 업데이트는 각 서버에서 새 `agent/` 폴더로 `install-agent.sh`(또는 `install-agent-user.sh`)를 같은 인자로 다시 실행하세요.

### DB 초기화 (모든 데이터 삭제)

⚠️ Todo, 출퇴근·휴가, GPU 예약, `/gpu link` 연결, 논문 주제·연구실 소개, 목표 학회 등 **모든 기록이 사라집니다.** 먼저 백업해 두세요.

```bash
# Docker: 반드시 봇을 먼저 끄고 지웁니다
docker compose down
cp data/labbot.db data/labbot.db.bak-$(date +%Y%m%d)   # 백업 (선택)
rm -f data/labbot.db data/labbot.db-wal data/labbot.db-shm
docker compose up -d
# systemd: sudo systemctl stop labbot → /opt/labbot/labbot.db* 삭제 → sudo systemctl start labbot
```

* **봇이 켜진 채로 지우면 안 됩니다.** 실행 중인 봇은 이미 열어 둔 파일에 계속 쓰기 때문에 재시작 전까지 예전 데이터가 그대로 보입니다.
* `data/google-sa.json`(서비스 계정 키)은 지우지 마세요.
* DB 위치는 `DB_PATH`로 정해집니다. Docker는 `/data/labbot.db`(= 호스트의 `data/labbot.db`)입니다. 봇이 여러 곳에서 돌고 있다면 **실제로 돌고 있는 서버**의 파일을 지워야 합니다.
* 다시 켜면 빈 DB가 만들어지고 학회 목록은 `conferences.yaml`에서 다시 읽습니다. 학생들은 `/gpu link`, 관리자는 `/papers add`·`/papers profile`을 다시 해 주세요.

### lb (학습 작업 알림) 설정과 테스트

1. NAS의 `.env`에 `JOBS_TOKEN`을 넣습니다(`openssl rand -hex 24`, `METRICS_TOKEN`과 **다른 값**). 그다음 봇을 다시 켭니다(`docker compose up -d`). 로그에 `JOBS_TOKEN이 비어 있어…` 경고가 없으면 켜진 것입니다.
2. Slack 앱에 최신 `manifest.yaml`을 반영해 `/jobs` 명령을 추가합니다.
3. 각 GPU 서버에서 마지막 인자로 `JOBS_TOKEN`을 주고 에이전트를 다시 설치합니다. `lb`가 함께 설치됩니다.
   ```bash
   sudo ./install-agent.sh http://<NAS-IP>:8787/metrics <METRICS_TOKEN> gpu1 /,/home,/data <JOBS_TOKEN>
   # sudo 없이: bash ./install-agent-user.sh (같은 인자) → ~/.local/bin/lb
   ```
4. Slack에서 `/gpu link <리눅스계정>`으로 계정을 연결합니다.
5. GPU 서버에서 테스트합니다.
   ```bash
   lb notify "lb 테스트"                          # 바로 DM이 오면 연결 성공
   lb run --min 0 -- sleep 5                      # ✅ 성공 알림 (기본 --min 1m: 1분 미만 성공은 알리지 않음)
   lb run -- python -c "import sys; sys.exit(3)"  # ❌ 실패 알림 (실패는 항상 알림)
   lb run --min 0 -- python -c "import torch, time; x = torch.ones(1, device='cuda'); time.sleep(90)"  # GPU 사용량 집계
   ```
   마지막 명령이 도는 동안 Slack에서 `/jobs`, `/jobs all`을 입력하면 실행 중인 작업이 보여야 합니다.

**DM이 안 올 때**
* `lb`가 출력하는 경고를 먼저 보세요. `401`이면 봇 `.env`에 `JOBS_TOKEN`이 없거나, 서버의 `/etc/labbot-lb.conf`(또는 `~/.config/labbot-lb.conf`)에 적힌 값과 다른 것입니다.
* 연결 자체가 안 되면 GPU 서버에서 `curl http://<NAS-IP>:8787/health`로 방화벽(8787 포트)을 확인하세요.
* `lb` 시작 시 계정 연결 경고가 나오면 `/gpu link`를 안 한 것입니다.

---

## 사용법

### 📝 Todo
| 명령 | 설명 |
|---|---|
| `/todo review paper tomorrow 14:00` | 내 할 일 추가 (`/todo add ...`도 같음, `/todo add`만 치면 입력 창) |
| `/todo clean up results @홍길동 @김철수 by friday` | 다른 사람에게 맡기기 (맡은 사람에게 DM이 갑니다) |
| `/todo list` · `/todo sent` · `/todo channel` | 내 할 일 · 내가 맡긴 일 · 이 채널의 할 일 |
| `/todo done 12` · `/todo delete 12` | 번호로 완료 또는 삭제 (`/todo done 12 13` 처럼 여러 개 가능) |
| 메시지 `⋮` → **Todo로 만들기** | 메시지를 할 일로 저장 (원문 링크 포함) |

* 마감 표현: `today` `tomorrow` `fri` `by friday` `next tue` `10/5` `by 11/5` `2026-10-05` `14:00` `3pm` `at 3:30pm`
  * 한국어 `내일` `금요일까지` `다음주 화요일` `오후 3시` 도 그대로 알아듣습니다.
  * `sat`, `sun`은 일반 단어와 겹칠 수 있어 `saturday`, `sunday`처럼 전체 이름으로 써 주세요.
* 알림: 마감 시각이 있으면 30분 전, 날짜만 있으면 그날 오전 9시에 DM이 옵니다. 알림에서 `1시간 뒤`, `내일 9시`로 미룰 수 있습니다.
* 평일 오전 9시에는 오늘 마감이거나 기한이 지난 일을 모아서 보내 줍니다.
* 맡긴 일이 완료되면 요청한 사람에게 알림이 갑니다.

### 🕘 출퇴근 · 휴가
| 명령 | 설명 |
|---|---|
| `/in` · `/in remote` · `/in office conference` | 출근 기록 (`office` 출근 · `remote` 재택, 뒤의 글은 메모 / `#출퇴근` 채널에 공유) |
| `/in 09:10` · `/out 18:30` | 깜빡했을 때 실제 시각으로 기록하거나 정정 |
| `/pause` · `/pause 점심` | 휴식 시작. 다시 `/pause`(또는 `/in`, 홈 탭 ▶️ 복귀)로 복귀. **휴식 시간은 근무 시간에서 빠집니다** |
| `/pause 12:00-13:00 점심` | 지난 휴식을 나중에 기록 (퇴근 후에도 가능) |
| `/out` | 퇴근 기록과 오늘 근무 시간 표시 |
| `/vacation` | 휴가 신청 창 (연차, 오전·오후 반차, 병가, 공가, 기타) |
| `/vacation list` · `/vacation team` | 내 휴가와 올해 연차 사용 일수 · 앞으로 2주간 팀 휴가 |
| `/attendance` | 오늘 누가 출근·재택·휴가·미출근인지 |
| `/attendance week` · `/attendance month @홍길동` | 근무일, 총 근무시간, 평균 출근 시각, 퇴근 기록 누락 |
| `/attendance export 2026-09` | 월별 출퇴근과 휴가 기록 CSV를 DM으로 받기 (관리자만, 엑셀에서 한글이 깨지지 않습니다) |
| `/attendance sheet` | 출퇴근·휴가 Google Sheet 바로 동기화 (관리자만) |

* **휴가 승인 절차:** 신청하면 `LEAVE_APPROVER_IDS`에게 승인·반려 버튼이 담긴 DM이 갑니다. 처리 결과는 신청자에게 DM으로, 승인된 휴가는 `#출퇴근` 채널에 공유됩니다. 반려할 때는 사유를 적을 수 있습니다. 승인자 본인이 신청하면 자동으로 승인됩니다.
* 휴가 일수는 주말과 **공휴일(대체공휴일 포함)을 빼고** 계산합니다. 연구실 자체 휴무일은 `EXTRA_HOLIDAYS`에 추가하세요.
* **미출근 리마인더:** 평일 10:30에 출근 기록이 없고 휴가도 아닌 사람에게 `출근`·`재택`·`휴가 신청` 버튼이 담긴 DM이 갑니다. 공휴일에는 보내지 않고, 오후 반차인 사람에게는 보냅니다.
* **미퇴근 리마인더:** 21:00에 퇴근 기록이 없는 사람에게 DM이 갑니다.
* 봇의 **App Home 탭**에서 버튼 하나로 출퇴근하고, 할 일과 GPU 현황을 한눈에 볼 수 있습니다.

### 🖥 서버
| 명령 | 설명 |
|---|---|
| `/gpu` · `/gpu free` | GPU별 사용률·메모리·온도·사용자 · 비어 있는 GPU만 |
| `/gpu reserve gpu1 0,1 3h paper experiment` | GPU 예약 (`0-3`, `all` / 기간 `90m` `3h` `2d`, 최대 7일) |
| `/gpu release` · `/gpu mine` | 내 예약 해제 · 내 예약 보기 |
| `/gpu summary` · `/gpu today` | 어제 · 오늘 사용 요약 |
| `/gpu week` · `/gpu week now` | 지난주 · 이번 주 GPU 리포트 (예약 vs 실제 사용량) |
| `/gpu week sheet` | 지난주 통계를 Google Sheet에 기록 (관리자, 이미 기록했으면 건너뜀. `force`로 다시 기록) |
| `/server` | 서버별 CPU, 메모리, 디스크 |
| `/gpu forget old-name` | 이름을 바꿨거나 없앤 서버를 현황에서 지우기 (관리자, 지난 통계는 유지) |

자동 알림 (`#서버알림`)
* 💤 **GPU 메모리만 잡고 쓰지 않는 프로세스**: 사용률 5% 이하 상태가 60분 넘게 이어지면 채널에 알리고, 계정이 연결된 본인에게 DM을 보냅니다. 남아 있는 주피터 커널을 잡는 데 유용합니다.
* 🔒 다른 사람이 예약한 GPU에서 내 프로세스가 아닌 프로세스가 돌고 있을 때
* 💾 디스크 사용량 95% 초과 (6시간마다 다시 알림, `DISK_ALERT_PCT`)
* 🔴 서버나 에이전트가 5분 넘게 응답하지 않을 때, ✅ 다시 연결됐을 때
* 📊 매일 09:00 어제 사용 요약: 서버별·GPU별 평균 사용률, **사용자별 GPU 사용 시간(그중 놀고 있던 시간)**, 디스크 남은 공간
* 📈 **매주 월요일 09:30 주간 리포트** (지난주 월~일)
  * 전체·서버별 평균 사용률, 메모리 사용률, 실제로 사용 중이던 시간 비율
  * 사용자별 GPU·시간, 평균 사용률, 평균·최대 메모리, 유휴 점유 시간
  * **예약 vs 실제 사용**: 사람별 예약 건수와 예약 GPU·시간, 그 시간 중 GPU가 실제로 쓰인 비율과 평균 사용률, 본인 프로세스가 돈 시간. 예약 시간의 30% 미만만 썼으면 ⚠️
  * '사용 중'은 사용률이 `FREE_GPU_UTIL` 이상이거나 메모리가 `FREE_GPU_MEM_MB` 이상인 시간입니다. 일찍 해제한 예약은 해제 시각까지만 셉니다.

모든 기준값은 `.env`에서 바꿀 수 있습니다.

### 🏃 학습 작업 종료 알림 (`lb`)

**학습 명령 앞에 `lb run --` 만 붙이면 끝.** 작업이 끝나면 Slack DM이 옵니다.

```bash
lb run -- python train.py --lr 1e-4
lb run --name exp1-lr1e-4 --every 6h -- torchrun --nproc_per_node=4 train.py   # 이름 지정, 6시간마다 중간 보고
nohup lb run -- python train.py > train.log 2>&1 &                              # nohup·tmux·스크립트 안에서도 그대로
lb run -- docker run --gpus all -v $PWD:/w img python /w/train.py   # 컨테이너도 그대로
lb run -- docker run -d --gpus all img python train.py              # -d면 바로 돌아오고, 컨테이너가 끝날 때 알림
lb docker exp1                      # 이미 떠 있는 컨테이너 감시 (종료 코드·로그·GPU 사용량까지)
lb watch 12345 --log train.log      # lb 없이 이미 돌리고 있는 작업 감시 (종료 코드는 알 수 없어 "끝남"으로 표시)
lb notify "eval 끝남: BLEU 31.2"     # 스크립트 안에서 한 줄 알림
```

* 출력은 터미널에 그대로 보이고(진행 막대·색 포함), **종료 코드도 그대로** 돌려줍니다. `Ctrl+C`·`SIGTERM`은 학습 프로세스에 전달됩니다.
* `--min 1m`(기본): 이보다 짧게 끝난 **성공** 작업은 알리지 않습니다. **실패는 항상** 알립니다.
* 끝나면 DM에 ✅/❌, 서버, 걸린 시간, 종료 코드(시그널 이름), **로그 마지막 20줄**, 사용한 GPU 번호·최대 메모리·평균 사용률이 담깁니다.
  * 흔한 실패는 첫 줄에 원인을 적습니다: CUDA out of memory, exit 137/Killed(OOM killer), loss NaN·inf, No space left on device, ModuleNotFoundError
  * 평균 GPU 사용률이 `JOBS_LOW_UTIL`(10%)보다 낮으면 "데이터 로딩 병목일 수 있어요"라고 알려 줍니다.
  * GPU 사용량은 에이전트가 보내는 프로세스 정보에서 **작업의 pid와 그 자식 프로세스**(torchrun worker 등)를 찾아 집계합니다. Docker 컨테이너는 `lb`의 자식 프로세스가 아니라서, `lb`가 `docker run`에 `--cidfile`을 끼워 넣어 컨테이너 ID를 알아낸 뒤 컨테이너 메인 프로세스의 pid를 봇에 따로 알립니다. `docker exec`로 나중에 띄운 프로세스는 메인 프로세스의 자식이 아니라 집계되지 않고, 컨테이너 **안에서** `lb`를 실행하면 pid가 달라 집계되지 않습니다.
  * `docker run -d`는 바로 끝나므로, `lb`가 컨테이너를 기다리는 `lb docker`를 백그라운드로 띄워 두고 돌아옵니다. 종료 코드는 `docker wait`, 로그는 `docker logs`에서 가져옵니다. `--rm` 컨테이너가 감시 시작 전에 이미 지워졌다면 종료 코드는 "알 수 없음"이 됩니다.
  * `lb docker`를 Ctrl+C로 멈추면 감시만 멈추고 컨테이너는 계속 돕니다. `lb run -- docker run`(포그라운드)의 Ctrl+C는 평소처럼 `docker run`에 전달됩니다.
* 로그와 명령에 섞인 비밀값(`sk-…`, `hf_…`, wandb·AWS 키, `token=`, `password=` 등)은 **서버에서 보내기 전에** `***`로 가립니다.
* 작업이 도는 서버가 응답 없음 상태가 되면 작업 주인에게 "gpu1 응답이 끊겼어요" DM이 갑니다 (작업당 한 번).
* 봇이 꺼져 있거나 연결이 안 되면 3번 재시도한 뒤 경고만 출력하고, 작업은 그대로 진행합니다.
* 처음 쓰기 전에 Slack에서 `/gpu link <리눅스계정>`을 해 주세요. 연결이 안 돼 있으면 `lb`가 시작할 때 경고합니다.

| 명령 | 설명 |
|---|---|
| `/jobs` | 내 실행 중인 작업과 최근 10개 결과 |
| `/jobs all` | 연구실 전체 실행 중인 작업 (이름, 서버, GPU, 경과 시간만. 로그는 보여 주지 않음) |

**설치와 인증**
* `install-agent.sh`의 다섯 번째 인자로 `JOBS_TOKEN`을 주면 `/usr/local/bin/lb`와 `/etc/labbot-lb.conf`(0644, 봇 주소·`JOBS_TOKEN`·서버 이름)를 만듭니다. sudo 없이 설치하면 `~/.local/bin/lb`, `~/.config/labbot-lb.conf`로 내 계정에만 설치됩니다.
* `JOBS_TOKEN`은 모든 사용자가 읽을 수 있어서 **`POST /jobs`에만** 쓸 수 있습니다. 지표(`/metrics`)는 root만 읽는 `METRICS_TOKEN`으로만 받습니다. 두 값이 같으면 봇이 시작하지 않습니다.
* ⚠️ **연구실 내부용이라 사용자 위조는 막지 않습니다.** 서버에 로그인한 사람은 다른 사람 이름으로 작업 알림을 보낼 수 있습니다 (예: `USER=kim lb notify ...`). 알림 내용을 근거로 무언가를 승인하거나 처리하지 마세요.
* 기존 서버는 새 `agent/` 폴더로 `install-agent.sh`를 다시 실행하면 됩니다. 에이전트도 함께 갱신해야 자식 프로세스의 GPU 사용량이 집계됩니다 (예전 에이전트는 `lb`가 직접 띄운 프로세스만 찾음).

---

### 🗓 미팅 action items
| 명령 | 설명 |
|---|---|
| `/meeting` | 회의록을 붙여 넣으면 **요약·결정 사항·action items(담당자·마감)** 초안을 DM으로 보내 줍니다 |
| 스레드 메시지 `⋮` → **미팅 정리** | 스레드 전체를 회의록으로 불러오고, 스레드 참여자를 참석자로 채워 줍니다 |
| `/meeting recurring @김철수 tue 14:00 60 1:1 meeting` | 정기 미팅 등록. **미팅이 끝나면 참석자에게 "지금 정리하기" 버튼**이 담긴 DM이 갑니다 |
| `/meeting recurring` · `/meeting list` · `/meeting view 12` | 정기 미팅 관리 · 최근 미팅 · 미팅 기록 보기 |
| `/meeting weekly` | 이번 주 내 미팅 action items 정리 |

* 초안을 확인한 뒤 **✅ 등록**을 누르면 항목마다 담당자의 Todo가 생기고(마감 알림 포함), 참석자 모두에게 미팅 기록이 DM으로 갑니다. **✏️ 수정**에서는 `이름 | 할 일 | 마감` 형식으로 고칠 수 있습니다 (예: `김철수 | ablation 실험 | fri 14:00`).
* "다음 미팅까지", "금요일까지" 같은 표현은 **미팅 날짜 기준**으로 실제 날짜로 바꿉니다.
* 공유 범위는 기본이 참석자 DM이고, 원하면 `#미팅` 채널에도 게시할 수 있습니다. 개인 미팅 내용은 기본적으로 채널에 올라가지 않습니다.
* **매주 금요일 16:00 주간 정리:**
  * 각자에게 DM이 갑니다. 이번 주 미팅 횟수, 완료한 항목, 남은 항목(완료 버튼 포함), 기한 지난 항목, **아직 정리하지 않은 정기 미팅**을 보여 줍니다.
  * `#미팅` 채널에는 사람별 숫자만 올라갑니다 (미팅 수, 완료, 남음, 기한 지남). 내용은 올라가지 않습니다.
* API 키가 없으면 `- [ ]`, `TODO`, `→`, `~까지`가 들어간 줄을 action item으로 뽑습니다.

### 📚 논문
| 명령 | 설명 |
|---|---|
| `/papers add video diffusion, 3D generation cat:cs.CV` | 연구실 주제 추가 (쉼표는 '또는', `cat:`는 arXiv 분야 제한) |
| `/papers add-mine neural rendering` | 개인 주제 추가 (결과가 DM으로 옵니다) |
| `/papers profile <연구실 소개>` | Claude가 관련도를 판단할 기준 (관리자) |
| `/papers profile clear` | 연구실 소개 삭제 (관리자) |
| `/papers list` · `/papers delete 3` | 주제 보기·삭제 |
| `/papers search test-time scaling` | 최근 2주 논문을 바로 찾아 요약 |
| `/papers summary 2609.12345` | LabBot이 소개한 논문(다이제스트·검색 결과)을 문제·방법·결과·한계로 자세히 요약. 그 밖의 논문은 요약하지 않습니다 |
| `/papers now` | 다이제스트 바로 실행 (관리자, 설정 확인용) |

* **매일 평일 08:30**: 주제별 arXiv 신규 논문과 **Hugging Face Daily Papers**(커뮤니티 추천 수 🔥)를 모아서 Claude가 연구실 관점의 관련도(0~10)를 매기고, Top 5를 한국어 요약과 함께 `#논문`에 올립니다.
* **매주 금요일 17:00**: 이번 주에 소개한 논문 중 관련도가 높은 Top 5를 다시 모아 보여 줍니다.
* `PAPER_SCHEDULE`로 `daily` / `weekly` / `both` / `off`를 고를 수 있습니다.
* 각 논문에는 두 버튼이 붙습니다.
  * **📌 읽을 목록**: 내 Todo에 "논문 읽기"로 추가
  * **🔍 자세히**: 스레드에 상세 요약 (한 번 만든 요약은 저장해 두고 다시 씁니다)
* 한 번 소개한 논문은 다시 보내지 않습니다.
* 요약은 **초록을 근거로** 합니다. 본문 PDF는 읽지 않습니다.

### 🎯 학회 마감
| 명령 | 설명 |
|---|---|
| `/deadline` | 앞으로 90일 안의 마감 (D-day 순). 날짜가 아직 없는 학회도 아래에 표시 |
| `/deadline target ICLR Efficient DiT` | 내 목표 학회 등록 (논문 제목은 선택). D-30·14·7·3·1·당일 오전 9시에 DM |
| `/deadline untarget ICLR` · `/deadline mine` | 목표 해제 · 내 목표 학회와 D-day |
| `/deadline targets` | 누가 어느 학회를 준비 중인지 한눈에 |
| `/deadline set ICLR 2027 paper 2026-10-01 23:59 AoE` | 마감 추가·수정. 시각을 빼면 23:59, 시간대를 빼면 학회 기본 시간대 |
| `/deadline unset ICLR 2027 abstract` | 마감 삭제 |
| `/deadline conf list` | 그룹별 학회 목록과 다음 마감 |
| `/deadline conf add` | 입력 창으로 학회 추가. 빠른 추가: `/deadline conf add KDD group:ML url:https://kdd.org tz:AoE` |
| `/deadline conf edit KDD` | 학회 정보와 **마감 목록**을 입력 창에서 수정 (줄을 지우면 그 마감 삭제) |
| `/deadline conf remove KDD` | 확인 창에서 목표로 등록한 사람을 보여 준 뒤 삭제. 관련 목표와 알림도 함께 정리하고, 그분들께 DM |
| `/deadline conf watch KDD` · `unwatch` | 월요일 채널 요약에 포함할지 |
| `/deadline conf export` | 현재 목록과 마감을 yaml 파일로 DM (백업·이전용) |

* **마감 종류:** `abstract` `paper` `supplementary` `arr_cycle` `commitment`. 초록 마감은 본 마감보다 먼저, 굵게 표시합니다.
* **시간대:** `AoE`(UTC-12) `UTC-7` `UTC+5:30` `KST` `PT` `America/Los_Angeles` 등. 모든 시각은 KST로 바꿔 보여 주고 원래 시간대도 함께 적습니다.
  예: `ICLR 2027 논문 마감 · 10/2(금) 20:59 KST (AoE 10/1 23:59) · D-7` (AoE 23:59는 KST로 다음 날 20:59)
* **ARR 학회** (ACL, EMNLP, NAACL, EACL, AACL)
  * ARR 주기(`ARR 2027 Jan` 같은 항목, 종류 `arr_cycle`)와 학회의 `commitment` 마감을 따로 관리합니다.
  * 각 ARR 주기에 "이 주기 결과로 commit할 수 있는 학회"를 연결합니다. 연결은 `/deadline conf edit ARR 2027 Jan` 입력 창에서 고칩니다.
  * ARR 학회를 목표로 등록하면 **가장 가까운 ARR 주기 투고 마감과 commitment 마감을 둘 다** 알려 줍니다.
  * 새 주기 추가 예: `/deadline conf add ARR 2027 Feb group:ARR type:arr_cycle commit:ACL,EMNLP` 다음 `/deadline set ARR 2027 Feb arr_cycle 2027-02-15 23:59`
* **권한:** 학회 추가는 누구나 할 수 있습니다. 수정·삭제·watch 변경·마감 추가는 그 학회를 추가한 사람이나 관리자(`ADMIN_IDS`)만 할 수 있습니다. 초기 목록의 학회는 관리자만 고칠 수 있습니다.
* 학회 이름은 대소문자와 공백을 무시하고 찾습니다 (`iclr` = `ICLR`). 오타가 있으면 "혹시 *ICLR*?" 처럼 제안합니다.
* **알림**
  * 마감 날짜가 바뀌거나 새로 등록되면 목표로 등록한 사람에게 바로 DM이 갑니다 (예: "ICLR 2027 논문 마감 이 10/1 → 10/3으로 바뀌었어요").
  * 같은 마감에 대한 알림은 하루에 한 번만 갑니다. 이미 지난 마감은 알리지 않습니다.
  * 매주 월요일 09:00 `DEADLINE_CHANNEL`에 앞으로 6주 안의 마감을 요약합니다 (watch가 켜진 학회만, 마감이 없으면 올리지 않음).
  * 금요일 미팅 주간 정리와 App Home에도 "목표 학회까지 D-n"이 표시됩니다.
  * 모든 알림에는 공식 사이트 링크와 "공식 사이트에서 날짜를 꼭 확인하세요" 문구가 붙습니다.
* **초기 목록 (`app/conferences.yaml`)**
  * 봇을 **처음 실행할 때 한 번만** 읽습니다. 그 뒤로는 DB가 기준이라 yaml을 고쳐도 반영되지 않습니다. Slack에서 고치세요.
  * 날짜는 공식 사이트에서 확인한 것만 넣었고, 각 날짜 옆에 출처 URL을 주석으로 달았습니다. 아직 발표되지 않은 학회는 `TODO`로 비워 뒀습니다.
  * 다른 서버로 옮길 때는 `/deadline conf export`로 받은 파일을 새 봇의 `app/conferences.yaml`(또는 `DEADLINE_SEED_FILE`)로 두고 빈 DB로 시작하면 됩니다.

## Slurm으로 가는 징검다리

Slurm이 부담스러운 동안 `/gpu reserve`를 **약속 기반 예약 시스템**으로 써 보세요. 예약 현황이 채널에 공유되고, 예약된 GPU를 다른 사람이 쓰면 알림이 가기 때문에 "누가 몇 번 GPU를 쓰는지" 정리하는 습관이 먼저 잡힙니다. 사용자별 GPU 사용 시간과 유휴 점유 통계가 쌓이면 Slurm 도입이 왜 필요한지 설득하는 근거로도 쓸 수 있습니다.

---

## 개발

```bash
pip install -r requirements.txt pytest
python -m pytest -q tests     # Slack API는 가짜로 대체해 실행합니다
python -m app.main            # .env 필요
```

```
app/
  main.py        진입점 (Socket Mode, 스케줄러, 지표 수신 서버)
  todo.py        /todo, 메시지 단축키, 리마인더
  attendance.py  /in /pause /out /vacation /attendance, 승인 절차, 리마인더, CSV
  servers.py     지표 수신(HTTP), /gpu /server, 예약, 알림, 일일 요약
  gpureport.py   주간 GPU 리포트 (예약 vs 실제 사용), Google Sheet 기록
  meetings.py    /meeting, 회의록 → action items, 정기 미팅 알림, 금요일 주간 정리
  papers.py      /papers, arXiv·HF 수집, 선별·요약, 매일·주간 다이제스트
  deadlines.py   /deadline, 학회 목록·마감·목표 학회, D-day 알림, 월요일 요약
  jobs.py        /jobs, lb 작업 이벤트(POST /jobs) 처리, 종료 DM, 실패 원인 힌트, 작업별 GPU 집계
  conferences.yaml  학회 초기 목록 (첫 실행에만 읽음)
  llm.py         Claude API 래퍼 (도구 호출로 JSON 구조화 출력)
  home.py        App Home 탭
  timeparse.py   한국어 날짜·시간 파싱
agent/           각 서버에 설치하는 수집기와 lb CLI (표준 라이브러리만 사용)
docs/BACKLOG.md  다음에 만들 기능 후보
```

데이터는 SQLite 파일(`labbot.db`) 하나에 저장됩니다. 이 파일만 백업하면 됩니다.
