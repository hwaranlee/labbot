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
6. ID 확인 방법
   * 채널 ID: 채널 이름 클릭 → 맨 아래 `C0...`
   * 사람 ID: 프로필 → `⋮` → **멤버 ID 복사** (`U0...`)

> **이미 앱을 만들었다면** 새 `manifest.yaml`로 앱 설정의 **App Manifest**를 덮어쓰고 저장한 뒤, **Install App → Reinstall** 해 주세요. `/meeting`·`/papers` 명령과 스레드를 읽는 권한(`channels:history`)이 추가됐습니다.

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
# GPU 서버 1
sudo ./install-agent.sh http://<NAS-IP>:8787/metrics <METRICS_TOKEN> gpu1 /,/home,/data
# GPU 서버 2
sudo ./install-agent.sh http://<NAS-IP>:8787/metrics <METRICS_TOKEN> gpu2 /,/home,/data
# NAS (봇과 같은 서버여도 따로 설치. 주소는 127.0.0.1로)
sudo ./install-agent.sh http://127.0.0.1:8787/metrics <METRICS_TOKEN> nas /volume1
```

* **sudo 권한이 없는 서버**에서는 같은 인자로 `bash ./install-agent-user.sh ...`를 쓰세요. 내 계정의 systemd 사용자 서비스(안 되면 crontab)로 등록하고, root 없이도 모든 사용자의 GPU 프로세스를 볼 수 있습니다.
* 네 번째 인자는 감시할 디스크 경로이고, 서버마다 실제 마운트 경로로 바꿔 주세요 (`df -h`로 확인).
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
* 💾 디스크 사용량 90% 초과 (6시간마다 다시 알림)
* 🔴 서버나 에이전트가 5분 넘게 응답하지 않을 때, ✅ 다시 연결됐을 때
* 📊 매일 09:00 어제 사용 요약: 서버별·GPU별 평균 사용률, **사용자별 GPU 사용 시간(그중 놀고 있던 시간)**, 디스크 남은 공간
* 📈 **매주 월요일 09:30 주간 리포트** (지난주 월~일)
  * 전체·서버별 평균 사용률, 메모리 사용률, 실제로 사용 중이던 시간 비율
  * 사용자별 GPU·시간, 평균 사용률, 평균·최대 메모리, 유휴 점유 시간
  * **예약 vs 실제 사용**: 사람별 예약 건수와 예약 GPU·시간, 그 시간 중 GPU가 실제로 쓰인 비율과 평균 사용률, 본인 프로세스가 돈 시간. 예약 시간의 30% 미만만 썼으면 ⚠️
  * '사용 중'은 사용률이 `FREE_GPU_UTIL` 이상이거나 메모리가 `FREE_GPU_MEM_MB` 이상인 시간입니다. 일찍 해제한 예약은 해제 시각까지만 셉니다.

모든 기준값은 `.env`에서 바꿀 수 있습니다.

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
  llm.py         Claude API 래퍼 (도구 호출로 JSON 구조화 출력)
  home.py        App Home 탭
  timeparse.py   한국어 날짜·시간 파싱
agent/           각 서버에 설치하는 수집기 (표준 라이브러리만 사용)
```

데이터는 SQLite 파일(`labbot.db`) 하나에 저장됩니다. 이 파일만 백업하면 됩니다.
