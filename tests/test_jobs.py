"""학습 작업 알림 테스트: lb CLI(실제 프로세스 실행)와 봇의 /jobs 처리."""
import getpass
import importlib.machinery
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app import jobs
from app.servers import start_receiver
from tests.test_labbot import KIM, ME, _load, env  # noqa: F401

LB = str(Path(__file__).resolve().parent.parent / "agent" / "lb")
_loader = importlib.machinery.SourceFileLoader("lb_cli", LB)
_spec = importlib.util.spec_from_loader("lb_cli", _loader)
lb = importlib.util.module_from_spec(_spec)
_loader.exec_module(lb)

USER = getpass.getuser()
PY = sys.executable


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_lb(args, conf_url="http://127.0.0.1:9/jobs", token="jt", timeout=60):
    """lb를 실제 프로세스로 실행 (기본: 아무도 듣지 않는 포트 = 봇 꺼짐)"""
    envv = dict(os.environ, LB_CONF="/nonexistent", LABBOT_JOBS_URL=conf_url, LABBOT_JOBS_TOKEN=token,
                LABBOT_HOST="gpu1")
    return subprocess.run([PY, LB] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=envv,
                          timeout=timeout, universal_newlines=True)


@pytest.fixture()
def bot(env):
    """실제 HTTP 수신 서버 + 이 테스트를 돌리는 리눅스 계정을 ME에 연결"""
    env.ctx.cfg.jobs_token = "jt"
    env.ctx.cfg.metrics_host = "127.0.0.1"
    env.ctx.cfg.metrics_port = _free_port()
    env.ctx.db.run("INSERT INTO user_links VALUES (?, ?)", (USER, ME))
    srv = start_receiver(env.ctx, env.mon)
    env.url = f"http://127.0.0.1:{env.ctx.cfg.metrics_port}"
    yield env
    srv.shutdown()


def _dms(env, uid=ME):
    return [a for a in env.client.of("chat.postMessage") if a["channel"] == "D" + uid]


def _text(msg) -> str:
    return msg["text"] + json.dumps(_load(msg.get("blocks") or []), ensure_ascii=False)


def _post(url, body, token):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


# ── lb CLI: 종료 코드·출력 그대로 ──────────────────────────
@pytest.mark.parametrize("code", [0, 1, 3])
def test_exit_code_passthrough_bot_down(code):
    """봇이 꺼져 있어도 작업은 정상 실행되고 종료 코드가 그대로"""
    t = time.time()
    r = run_lb(["run", "--", PY, "-c", f"import sys; print('hello out'); print('err line', file=sys.stderr); "
                                       f"sys.exit({code})"])
    assert r.returncode == code
    assert "hello out" in r.stdout and "err line" in r.stderr   # stdout·stderr를 섞지 않고 그대로
    assert "LabBot에 알림을 보내지 못했어요" in r.stderr
    assert time.time() - t < 20


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL, signal.SIGINT])
def test_signal_exit_passthrough(sig):
    """자식이 시그널로 죽으면 lb도 같은 시그널로 끝남 (부모 셸이 보는 결과가 같게)"""
    reset = f"signal.signal({int(sig)}, signal.SIG_DFL); " if sig != signal.SIGKILL else ""
    r = run_lb(["run", "--", PY, "-c", f"import os, signal; {reset}os.kill(os.getpid(), {int(sig)})"])
    assert r.returncode == -int(sig)


def test_signal_forwarded_to_child(tmp_path):
    """lb가 받은 SIGTERM은 자식에게 전달되고, 자식의 종료 코드가 그대로 나옴"""
    marker = tmp_path / "got"
    script = ("import signal, sys, time\n"
              "def h(s, f):\n"
              f"    open({str(marker)!r}, 'w').write(str(s)); sys.exit(42)\n"
              "signal.signal(signal.SIGTERM, h)\n"
              "print('ready', flush=True)\n"
              "time.sleep(30)\n")
    envv = dict(os.environ, LB_CONF="/nonexistent", LABBOT_JOBS_URL="http://127.0.0.1:9/jobs",
                LABBOT_JOBS_TOKEN="jt")
    p = subprocess.Popen([PY, LB, "run", "--", PY, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=envv, universal_newlines=True)
    assert p.stdout.readline().strip() == "ready"
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=30) == 42
    assert marker.read_text() == str(int(signal.SIGTERM))


def test_command_not_found():
    r = run_lb(["run", "--", "definitely-not-a-command-xyz"])
    assert r.returncode == 127 and "실행할 수 없어요" in r.stderr


def test_cli_parsing():
    assert lb.auto_name(["python", "-u", "train.py", "--lr", "1e-4"]) == "train.py"
    assert lb.auto_name(["torchrun", "--nproc_per_node=4", "scripts/pretrain.py"]) == "pretrain.py"
    assert lb.auto_name(["bash", "run_all.sh"]) == "run_all.sh"
    assert [lb.parse_duration(x) for x in ["90s", "1m", "6h", "1h30m", "2d", "0"]] == [90, 60, 21600, 5400, 172800, 0]
    with pytest.raises(ValueError):
        lb.parse_duration("soon")
    # 명령이 시작된 뒤의 --name 은 명령의 인자 (-- 없이 써도)
    opts, cmd = lb._parse_opts(["--name", "exp1", "python", "train.py", "--name", "x"], {"--name": None}, True)
    assert opts["--name"] == "exp1" and cmd == ["python", "train.py", "--name", "x"]
    opts, cmd = lb._parse_opts(["--min=0", "--", "python", "--min", "5"], {"--min": "1m"}, True)
    assert opts["--min"] == "0" and cmd == ["python", "--min", "5"]
    opts, rest = lb._parse_opts(["123", "--log", "a.log"], {"--log": None})      # lb watch: 옵션이 뒤에 와도 됨
    assert opts["--log"] == "a.log" and rest == ["123"]


def test_tail_keeps_last_lines_and_progress_bars():
    t = lb.Tail(n=3)
    t.feed(b"line1\nline2\n")
    t.feed(b"epoch 1:  10%\repoch 1:  50%\repoch 1: 100%\n", stream=2)
    t.feed(b"line4\nparti")
    t.feed(b"al", stream=1)
    t.feed(b"Traceback", stream=2)   # 다른 스트림의 부분 줄과 섞이지 않음
    assert t.get() == ["line4", "partial", "Traceback"]
    t2 = lb.Tail(n=50)
    t2.feed(b"".join(b"%d\n" % i for i in range(200)))
    assert t2.get() == [str(i) for i in range(150, 200)]


# ── 비밀값 마스킹 ──────────────────────────────────────────
@pytest.mark.parametrize("raw,secret", [
    ("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx", "sk-proj-abcdefghijklmnopqrstuvwx"),
    ("using key sk-ant-api03-AbCdEfGhIjKlMnOpQrSt", "AbCdEfGhIjKlMnOpQrSt"),
    ("hf token hf_AbCdEfGhIjKlMnOpQrStUvWxYz", "hf_AbCdEfGhIjKlMnOpQrStUvWxYz"),
    ("WANDB_API_KEY=0123456789abcdef0123456789abcdef01234567", "0123456789abcdef0123456789abcdef01234567"),
    ("wandb login 0123456789abcdef0123456789abcdef01234567", "0123456789abcdef0123456789abcdef01234567"),
    ("aws key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI/K7MDENG"),
    ("url?token=abc123secret&x=1", "abc123secret"),
    ("--password=hunter2", "hunter2"),
    ('"password": "p@ss w0rd"', "p@ss"),
    ("HF_TOKEN: abcdef", "abcdef"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x.y", "eyJhbGciOiJIUzI1NiJ9"),
])
def test_mask_secrets(raw, secret):
    out = lb.mask(raw)
    assert secret not in out and "***" in out


@pytest.mark.parametrize("text", [
    "max_tokens=512 num_tokens: 1024",
    "commit 0123456789abcdef0123456789abcdef01234567",
    "Epoch 3 | loss 0.231 | lr 1e-4",
    "skip-connections: true",
])
def test_mask_leaves_normal_logs(text):
    assert lb.mask(text) == text


def test_cmd_and_notify_are_masked(bot):
    r = run_lb(["run", "--min", "0", "--", PY, "-c", "print('token=supersecret123')", "--api-key=sk-abcdefghijklmnopqrstu"],
               conf_url=bot.url + "/jobs")
    assert r.returncode == 0
    j = bot.ctx.db.one("SELECT * FROM jobs")
    assert "supersecret123" not in j["tail"] and "sk-abcdefghijklmnopqrstu" not in j["cmd"]
    run_lb(["notify", "eval done, hf_AbCdEfGhIjKlMnOpQrStUvWxYz BLEU 31.2"], conf_url=bot.url + "/jobs")
    msg = _text(_dms(bot)[-1])
    assert "BLEU 31.2" in msg and "hf_AbCdEf" not in msg


# ── 실패 원인 힌트 ─────────────────────────────────────────
@pytest.mark.parametrize("tail,code,sig,expect", [
    (["Traceback ...", "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB"], 1, None, "GPU 메모리 부족"),
    (["RuntimeError: CUDA error: CUBLAS_STATUS_ALLOC_FAILED"], 1, None, "GPU 메모리 부족"),
    (["epoch 3 step 100", "Killed"], 137, None, "시스템 메모리 부족"),
    (["epoch 3 step 100"], 137, 9, "시스템 메모리 부족"),
    (["step 500 | loss: nan | lr 3e-4", "ValueError: stop"], 1, None, "NaN/inf"),
    (["train_loss=inf"], 1, None, "NaN/inf"),
    (["OSError: [Errno 28] No space left on device: 'ckpt/model.pt'"], 1, None, "디스크 공간 부족"),
    (["ModuleNotFoundError: No module named 'transformers'"], 1, None, "모듈 없음 (ModuleNotFoundError: transformers)"),
    (["ValueError: shapes do not match"], 1, None, None),
    (["loss 0.23 info: saved"], 1, None, None),
])
def test_classify_failures(tail, code, sig, expect):
    h = jobs.classify(tail, code, sig)
    if expect is None:
        assert h is None
    else:
        assert h and expect in h[0]


def test_exit_text():
    assert jobs.exit_text(1, None) == "exit 1"
    assert jobs.exit_text(137, None) == "exit 137 (SIGKILL)"
    assert jobs.exit_text(143, 15) == "exit 143 (SIGTERM)"
    assert jobs.exit_text(None, None) == "종료 코드 알 수 없음"


# ── 끝까지: lb → HTTP → DM ─────────────────────────────────
def test_end_to_end_success_and_failure(bot):
    r = run_lb(["run", "--min", "0", "--name", "exp1", "--", PY, "-c", "print('epoch 1 done')"],
               conf_url=bot.url + "/jobs")
    assert r.returncode == 0 and "epoch 1 done" in r.stdout and "알림을 보내지 못했어요" not in r.stderr
    msg = _text(_dms(bot)[-1])
    assert "✅" in msg and "exp1" in msg and "gpu1" in msg and "exit 0" in msg and "epoch 1 done" in msg

    r = run_lb(["run", "--", PY, "-c", "import sys; print('step 1'); "
                                       "print('torch.OutOfMemoryError: CUDA out of memory.', file=sys.stderr); "
                                       "sys.exit(1)"], conf_url=bot.url + "/jobs")
    assert r.returncode == 1
    dm = _dms(bot)[-1]
    first_line = _load(dm["blocks"])[0]["text"]["text"].splitlines()[0]
    assert first_line.startswith("❌") and "GPU 메모리 부족" in first_line   # 원인 힌트가 첫 줄에
    assert "```" in _text(dm) and "step 1" in _text(dm)
    j = bot.ctx.db.one("SELECT * FROM jobs WHERE status='failed'")
    assert j["exit_code"] == 1 and j["linux_user"] == USER and j["slack_id"] == ME


def test_min_duration(bot):
    """--min 미만으로 끝난 성공은 알리지 않고(기록은 남김), 실패는 항상 알림"""
    run_lb(["run", "--", PY, "-c", "pass"], conf_url=bot.url + "/jobs")            # 기본 --min 1m
    assert not _dms(bot)
    assert bot.ctx.db.one("SELECT status FROM jobs")["status"] == "ok"
    run_lb(["run", "--min", "10m", "--", PY, "-c", "raise SystemExit(2)"], conf_url=bot.url + "/jobs")
    assert len(_dms(bot)) == 1 and "❌" in _dms(bot)[0]["text"]


def test_unlinked_user_warned_but_runs(bot):
    bot.ctx.db.run("DELETE FROM user_links")
    r = run_lb(["run", "--min", "0", "--", PY, "-c", "print('ok')"], conf_url=bot.url + "/jobs")
    assert r.returncode == 0 and f"/gpu link {USER}" in r.stderr
    assert not _dms(bot)
    assert bot.ctx.db.one("SELECT status FROM jobs")["status"] == "ok"


def test_progress_every(bot):
    r = run_lb(["run", "--every", "1s", "--", PY, "-u", "-c",
                "import time\nfor i in range(3):\n    print('step', i); time.sleep(1)"], conf_url=bot.url + "/jobs")
    assert r.returncode == 0
    prog = [m for m in _dms(bot) if "실행 중" in m["text"]]
    assert prog and "step" in _text(prog[-1])


def test_watch_and_notify(bot, tmp_path):
    log = tmp_path / "train.log"
    log.write_text("epoch 1\nepoch 2\nfinal BLEU 31.2\n")
    p = subprocess.Popen([PY, "-c", "import time; time.sleep(1.5)"])
    envv = dict(os.environ, LB_CONF="/nonexistent", LABBOT_JOBS_URL=bot.url + "/jobs", LABBOT_JOBS_TOKEN="jt",
                LABBOT_HOST="gpu1")
    code = """import importlib.machinery, importlib.util, sys
loader = importlib.machinery.SourceFileLoader('lb_cli', sys.argv[1])
spec = importlib.util.spec_from_loader('lb_cli', loader); m = importlib.util.module_from_spec(spec)
loader.exec_module(m); sys.exit(m.watch(sys.argv[2:], poll=0.2))"""
    w = subprocess.Popen([PY, "-c", code, LB, str(p.pid), "--log", str(log), "--name", "old-run"], env=envv)
    p.wait()
    assert w.wait(timeout=30) == 0
    msg = _text(_dms(bot)[-1])
    assert "🏁" in msg and "old-run" in msg and "종료 코드 알 수 없음" in msg and "final BLEU 31.2" in msg
    r = run_lb(["notify", "eval 끝남: BLEU 31.2"], conf_url=bot.url + "/jobs")
    assert r.returncode == 0 and "eval 끝남: BLEU 31.2" in _dms(bot)[-1]["text"]


# ── 토큰 분리 ──────────────────────────────────────────────
def test_tokens_are_separated(bot):
    metrics = {"host": "gpu9", "interval": 60, "gpus": []}
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(bot.url + "/metrics", metrics, "jt")                 # JOBS_TOKEN으로 지표 쓰기 불가
    assert e.value.code == 401 and "gpu9" not in bot.mon.latest
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(bot.url + "/jobs", {"event": "notify", "host": "gpu1", "user": USER, "text": "x"}, "secret")
    assert e.value.code == 401
    assert _post(bot.url + "/metrics", metrics, "secret")[0] == 200
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(bot.url + "/jobs", {"event": "explode", "host": "gpu1", "user": USER}, "jt")
    assert e.value.code == 400


def test_jobs_disabled_without_token(env):
    env.ctx.cfg.metrics_host, env.ctx.cfg.metrics_port = "127.0.0.1", _free_port()
    srv = start_receiver(env.ctx, env.mon)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(f"http://127.0.0.1:{env.ctx.cfg.metrics_port}/jobs", {"event": "notify"}, "")
        assert e.value.code == 401
    finally:
        srv.shutdown()


# ── GPU 사용량: pid 매칭 ───────────────────────────────────
def _gpu_payload(procs_by_gpu, util=(90, 90, 90)):
    gpus = []
    for i, procs in enumerate(procs_by_gpu):
        gpus.append({"index": i, "name": "A6000", "util": util[i], "mem_used": sum(p["mem_mb"] for p in procs),
                     "mem_total": 49140, "procs": procs})
    return {"host": "gpu1", "interval": 60, "gpus": gpus}


def _start(env, job_id="a1b2c3d4", pid=1000, user="kim", host="gpu1"):
    return env.mon.jobs.handle({"event": "start", "job_id": job_id, "host": host, "user": user, "name": "train.py",
                                "pid": pid, "started_at": int(time.time()) - 3600, "cmd": "python train.py"})


def test_gpu_usage_by_pid_matching(env):
    env.ctx.db.run("INSERT INTO user_links VALUES ('kim', ?)", (KIM,))
    assert _start(env) == {"ok": True, "linked": True}
    _start(env, job_id="ffff0000", pid=2000, user="lee")
    for util in ((80, 60, 5), (100, 80, 5)):
        env.mon.ingest(_gpu_payload([
            [{"pid": 1001, "user": "kim", "mem_mb": 20000, "ppids": [1000, 900]}],          # torchrun worker
            [{"pid": 1002, "user": "kim", "mem_mb": 18000, "ppids": [1000, 900]},
             {"pid": 2000, "user": "lee", "mem_mb": 4000}],                                   # 다른 작업
            [{"pid": 3000, "user": "kim", "mem_mb": 9000, "ppids": [1]}],                      # lb 밖의 프로세스
        ], util=util))
    st = env.mon.jobs.stats("a1b2c3d4")
    assert st["gpus"] == [0, 1] and st["max_mem_mb"] == 38000 and st["max_gpu_mem_mb"] == 20000
    assert st["avg_util"] == pytest.approx(((80 + 60) / 2 + (100 + 80) / 2) / 2)
    assert env.mon.jobs.stats("ffff0000")["gpus"] == [1]

    env.mon.jobs.handle({"event": "end", "job_id": "a1b2c3d4", "host": "gpu1", "user": "kim", "exit_code": 0,
                         "tail": ["done"], "notify": True})
    msg = _text([a for a in env.client.of("chat.postMessage") if a["channel"] == "D" + KIM][-1])
    assert "GPU 0,1" in msg and "최대 메모리 37.1GB" in msg and "평균 사용률 80%" in msg
    assert "거의 쓰지 않았어요" not in msg
    assert "a1b2c3d4" not in env.mon.jobs.usage           # 끝나면 메모리에서 정리
    # 재전송된 end는 무시
    n = len(env.client.of("chat.postMessage"))
    env.mon.jobs.handle({"event": "end", "job_id": "a1b2c3d4", "host": "gpu1", "user": "kim", "exit_code": 0})
    assert len(env.client.of("chat.postMessage")) == n


def test_low_gpu_util_hint(env):
    env.ctx.db.run("INSERT INTO user_links VALUES ('kim', ?)", (KIM,))
    _start(env)
    for _ in range(4):
        env.mon.ingest(_gpu_payload([[{"pid": 1000, "user": "kim", "mem_mb": 9000}]], util=(3,)))
    env.mon.jobs.handle({"event": "end", "job_id": "a1b2c3d4", "host": "gpu1", "user": "kim", "exit_code": 0})
    msg = _text([a for a in env.client.of("chat.postMessage") if a["channel"] == "D" + KIM][-1])
    assert "GPU를 거의 쓰지 않았어요 (평균 3%). 데이터 로딩 병목일 수 있어요" in msg


def test_no_gpu_usage(env):
    env.ctx.db.run("INSERT INTO user_links VALUES ('kim', ?)", (KIM,))
    env.mon.jobs.handle({"event": "end", "job_id": "abcdef01", "host": "cpu1", "user": "kim", "exit_code": 0,
                         "name": "prep.py", "started_at": int(time.time()) - 120})   # start 없이 end만
    msg = _text([a for a in env.client.of("chat.postMessage") if a["channel"] == "D" + KIM][-1])
    assert "prep.py" in msg and "GPU 사용 기록 없음" in msg and "2분" in msg


# ── 서버 응답 없음 ─────────────────────────────────────────
def test_offline_host_alerts_job_owner_once(env):
    env.ctx.db.run("INSERT INTO user_links VALUES ('kim', ?)", (KIM,))
    env.mon.ingest(_gpu_payload([[]]))
    _start(env)
    _start(env, job_id="99998888", host="gpu2")
    env.mon.jobs.watch_job()
    assert not env.client.of("chat.postMessage")
    env.mon.latest["gpu1"]["recv"] -= 600
    env.mon.jobs.watch_job()
    env.mon.jobs.watch_job()
    dms = [a for a in env.client.of("chat.postMessage") if a["channel"] == "D" + KIM]
    assert len(dms) == 1 and "gpu1 응답이 끊겼어요" in dms[0]["text"]


# ── Slack 명령 · 홈 ────────────────────────────────────────
def test_jobs_commands_and_home(env):
    env.ctx.db.run("INSERT INTO user_links VALUES ('kim', ?)", (KIM,))
    _start(env)
    env.mon.ingest(_gpu_payload([[{"pid": 1000, "user": "kim", "mem_mb": 9000}]]))
    _start(env, job_id="ffff0000", pid=2000, user="lee")
    env.mon.jobs.handle({"event": "end", "job_id": "eeee1111", "host": "gpu1", "user": "kim", "exit_code": 1,
                         "name": "old.py", "tail": ["No space left on device"], "started_at": int(time.time()) - 60})
    env.command("/jobs", user=KIM)
    out = json.dumps(env.responded[-1]["blocks"], ensure_ascii=False)
    assert "내 실행 중인 작업* (1)" in out and "train.py" in out and "GPU 0" in out
    assert "old.py" in out and "디스크 공간 부족" in out
    env.command("/jobs", "all")
    out = json.dumps(env.responded[-1]["blocks"], ensure_ascii=False)
    assert "(2)" in out and "김철수" in out and "`lee`" in out and "No space" not in out and "1시간" in out
    env.command("/jobs", "help")
    assert "lb run --" in env.responded[-1]["text"]
    env.command("/jobs")
    assert "/gpu link" in json.dumps(env.responded[-1]["blocks"], ensure_ascii=False)   # ME는 미연결

    env.ctx.refresh_home(KIM)
    view = _load(env.client.of("views.publish")[-1]["view"])
    assert "내 실행 중인 작업 1개" in json.dumps(view["blocks"], ensure_ascii=False)


# ── docker ─────────────────────────────────────────────────
FAKE_DOCKER = r'''#!PY
"""테스트용 가짜 docker: run(-d·--cidfile) · inspect · wait · logs -f 만 흉내"""
import json, os, subprocess, sys, time, uuid
D = os.environ["FAKE_DOCKER_DIR"]
def load(ref):
    for f in os.listdir(D):
        c = json.load(open(os.path.join(D, f)))
        if c["id"].startswith(ref) or c["name"] == ref:
            return c
    sys.stderr.write("No such container: %s\n" % ref); sys.exit(1)
a = sys.argv[1:]
if a[0] == "run":
    a, detach, cidfile, name = a[1:], False, None, ""
    while a[0].startswith("-"):
        o = a.pop(0)
        if o in ("-d", "--detach"): detach = True
        elif o == "--cidfile": cidfile = a.pop(0)
        elif o == "--name": name = a.pop(0)
        elif o in ("--gpus", "-v", "-e"): a.pop(0)
    image, cmd = a[0], a[1:]
    cid = uuid.uuid4().hex + uuid.uuid4().hex
    c = {"id": cid, "name": name or cid[:6], "image": image, "pid": 424242, "start": time.time(),
         "log": [], "code": 0, "dur": 3600}   # 포그라운드: 명령이 끝날 때까지 실행 중
    if detach:   # 컨테이너 명령 대신 FAKE_* 환경변수로 결과를 정함
        c.update(log=os.environ.get("FAKE_LOG", "").split("|"), code=int(os.environ.get("FAKE_CODE", "0")),
                 dur=float(os.environ.get("FAKE_DUR", "2")))
    json.dump(c, open(os.path.join(D, cid), "w"))
    if cidfile:
        open(cidfile, "w").write(cid)
    if detach:
        print(cid); sys.exit(0)
    time.sleep(1.5)   # lb가 inspect할 시간
    sys.exit(subprocess.call(cmd))
c = load(a[-1])
done = time.time() >= c["start"] + c["dur"]
if a[0] == "inspect":
    f = a[a.index("-f") + 1]
    for k, v in (("{{.Id}}", c["id"]), ("{{.State.Pid}}", "0" if done else str(c["pid"])),
                 ("{{.State.Running}}", "false" if done else "true"), ("{{.Name}}", "/" + c["name"]),
                 ("{{.Config.Image}}", c["image"])):
        f = f.replace(k, v)
    print(f)
elif a[0] == "wait":
    time.sleep(max(0, c["start"] + c["dur"] - time.time())); print(c["code"])
elif a[0] == "logs":
    for ln in c["log"]:
        print(ln, flush=True)
    time.sleep(max(0, c["start"] + c["dur"] - time.time()))
'''


@pytest.fixture()
def fake_docker(tmp_path, monkeypatch):
    bindir, state = tmp_path / "bin", tmp_path / "containers"
    bindir.mkdir()
    state.mkdir()
    d = bindir / "docker"
    d.write_text(FAKE_DOCKER.replace("#!PY", "#!" + PY))
    d.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(state))
    return state


def _wait_done(env, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        j = env.ctx.db.one("SELECT * FROM jobs")
        if j and j["status"] != "running":
            return j
        time.sleep(0.2)
    raise AssertionError("작업이 끝나지 않았어요")


@pytest.mark.parametrize("cmd,detach,name", [
    ("docker run --gpus all -v /d:/d --rm -it img python train.py -d", False, "train.py"),   # 이미지 뒤 -d는 컨테이너 것
    ("sudo docker run -dit --name exp1 -p8080:80 -e=X img", True, "exp1"),
    ("docker container run --detach img python eval.py", True, "eval.py"),
    ("docker run --detach=false img", False, "img"),
    ("podman run -itv /a:/b nvcr.io/pytorch:24.01", False, "pytorch"),
])
def test_docker_run_detection(cmd, detach, name):
    info = lb.docker_run_info(cmd.split())
    assert info["detach"] is detach and lb.docker_name(cmd.split(), info) == name
    assert lb.docker_run_info("docker ps -a".split()) is None
    assert lb.docker_run_info("python docker run".split()) is None


def test_docker_foreground_attaches_container_pid(bot, fake_docker):
    """lb run -- docker run: 종료 코드·로그는 그대로, 컨테이너 메인 pid를 봇에 알려 GPU 매칭에 씀"""
    r = run_lb(["run", "--min", "0", "--", "docker", "run", "--gpus", "all", "img", PY, "-c",
                "import sys; print('in container'); sys.exit(3)"], conf_url=bot.url + "/jobs")
    assert r.returncode == 3 and "in container" in r.stdout
    j = bot.ctx.db.one("SELECT * FROM jobs")
    assert j["status"] == "failed" and json.loads(j["extra_pids"]) == [424242]
    assert "--cidfile" not in j["cmd"]   # 끼워 넣은 옵션은 보이지 않게
    assert "in container" in _text(_dms(bot)[-1])


def test_docker_detached_notifies_when_container_ends(bot, fake_docker, monkeypatch):
    """lb run -- docker run -d: 바로 돌아오고, 백그라운드 감시가 컨테이너 종료 코드·로그로 알림"""
    monkeypatch.setenv("FAKE_LOG", "epoch 1|torch.OutOfMemoryError: CUDA out of memory.")
    monkeypatch.setenv("FAKE_CODE", "1")
    t = time.time()
    r = run_lb(["run", "--name", "exp-d", "--", "docker", "run", "-d", "--gpus", "all", "img", "python", "train.py"],
               conf_url=bot.url + "/jobs")
    assert r.returncode == 0 and time.time() - t < 10 and "백그라운드 감시" in r.stderr
    assert len(r.stdout.strip()) == 64   # docker가 출력한 컨테이너 ID는 그대로
    j = _wait_done(bot)
    assert j["status"] == "failed" and j["exit_code"] == 1 and j["name"] == "exp-d"
    assert json.loads(j["extra_pids"]) == [424242]
    dm = _text(_dms(bot)[-1])
    assert "❌" in dm and "GPU 메모리 부족" in dm and "epoch 1" in dm


def test_lb_docker_existing_container(bot, fake_docker, monkeypatch):
    monkeypatch.setenv("FAKE_LOG", "step 9 loss 0.1")
    monkeypatch.setenv("FAKE_DUR", "3")
    subprocess.run(["docker", "run", "-d", "--name", "exp2", "img"], stdout=subprocess.PIPE, check=True)
    r = run_lb(["docker", "exp2"], conf_url=bot.url + "/jobs")
    assert r.returncode == 0 and "step 9" in r.stdout
    j = bot.ctx.db.one("SELECT * FROM jobs")
    assert j["status"] == "ok" and j["name"] == "exp2" and j["pid"] == 424242
    assert "✅" in _text(_dms(bot)[-1])
    assert run_lb(["docker", "nope"]).returncode == 2


def test_attach_adds_pids_for_gpu_matching(env):
    _start(env, pid=5000)   # docker CLI pid: GPU 프로세스의 조상이 아님
    env.mon.jobs.handle({"event": "attach", "job_id": "a1b2c3d4", "host": "gpu1", "user": "kim", "pids": [7000]})
    env.mon.ingest(_gpu_payload([[{"pid": 7100, "user": "root", "mem_mb": 12000, "ppids": [7000, 6900, 1]}]]))
    assert env.mon.jobs.stats("a1b2c3d4")["gpus"] == [0]
    with pytest.raises(ValueError):
        env.mon.jobs.handle({"event": "attach", "job_id": "a1b2c3d4", "host": "gpu1", "user": "kim", "pids": "x"})
