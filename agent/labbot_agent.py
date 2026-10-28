#!/usr/bin/env python3
"""LabBot 서버 에이전트 (표준 라이브러리만 사용, Python 3.6+).

GPU(nvidia-smi), CPU, 메모리, 디스크 사용량을 모아 LabBot으로 주기적으로 보냅니다.
GPU가 없는 서버(NAS 등)에서는 CPU/메모리/디스크만 보냅니다.

환경변수:
  LABBOT_URL      예) http://192.168.0.10:8787/metrics  (필수)
  LABBOT_TOKEN    봇의 METRICS_TOKEN과 같은 값 (필수)
  LABBOT_HOST     Slack에 표시할 서버 이름 (기본: hostname)
  LABBOT_DISKS    감시할 마운트 경로, 쉼표 구분 (기본: /)
  LABBOT_INTERVAL 전송 주기(초) (기본: 60)

점검:  python3 labbot_agent.py --once --print
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.0"


def read_cpu_times():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    return sum(vals), idle


class CpuMeter(object):
    def __init__(self):
        self.prev = read_cpu_times()

    def pct(self):
        cur = read_cpu_times()
        dt, di = cur[0] - self.prev[0], cur[1] - self.prev[1]
        self.prev = cur
        return round(100.0 * (dt - di) / dt, 1) if dt > 0 else 0.0


def mem_info():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])  # kB
    total = info.get("MemTotal", 0) / 1024.0
    avail = info.get("MemAvailable", info.get("MemFree", 0)) / 1024.0
    return {"total_mb": round(total), "used_mb": round(total - avail)}


def disks(mounts):
    out = []
    for m in mounts:
        if not os.path.exists(m):
            continue
        try:
            u = shutil.disk_usage(m)
        except OSError:
            continue
        out.append({"mount": m, "total_gb": round(u.total / 1024.0 ** 3, 1),
                    "used_gb": round((u.total - u.free) / 1024.0 ** 3, 1)})
    return out


def _run(cmd, timeout=20):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "exit %d" % p.returncode)
    return p.stdout


def _num(s):
    s = s.strip()
    try:
        return float(s)
    except ValueError:  # [N/A], [Not Supported]
        return None


def proc_info(pids):
    """pid -> {user, elapsed, cmd}"""
    if not pids:
        return {}
    out = {}
    try:
        txt = _run(["ps", "-o", "pid=,user:32=,etimes=,args=", "-p", ",".join(str(p) for p in pids)])
    except Exception:
        return out
    for line in txt.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        out[int(parts[0])] = {"user": parts[1], "elapsed": int(parts[2]) if parts[2].isdigit() else None,
                              "cmd": (parts[3] if len(parts) > 3 else "")[:200]}
    return out


def gpus():
    if not shutil.which("nvidia-smi"):
        return []
    q = "index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu"
    rows = _run(["nvidia-smi", "--query-gpu=" + q, "--format=csv,noheader,nounits"])
    result, by_uuid = [], {}
    for line in rows.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 7:
            continue
        g = {"index": int(f[0]), "name": f[2], "util": _num(f[3]) or 0.0,
             "mem_used": _num(f[4]) or 0.0, "mem_total": _num(f[5]) or 0.0, "temp": _num(f[6]), "procs": []}
        result.append(g)
        by_uuid[f[1]] = g
    try:
        apps = _run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory", "--format=csv,noheader,nounits"])
    except Exception:
        apps = ""
    procs = []
    for line in apps.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) >= 3 and f[1].isdigit():
            procs.append((f[0], int(f[1]), _num(f[2]) or 0.0))
    info = proc_info([p[1] for p in procs])
    for uuid, pid, mem in procs:
        g = by_uuid.get(uuid)
        if g is None:
            continue
        i = info.get(pid, {})
        g["procs"].append({"pid": pid, "user": i.get("user", "?"), "mem_mb": mem,
                           "elapsed": i.get("elapsed"), "cmd": i.get("cmd", "")})
    return result


def collect(cpu, mounts, interval, host):
    load = os.getloadavg()
    payload = {
        "host": host, "ts": int(time.time()), "interval": interval, "agent_version": VERSION,
        "cpu": {"pct": cpu.pct(), "cores": os.cpu_count(),
                "load1": round(load[0], 2), "load5": round(load[1], 2), "load15": round(load[2], 2)},
        "mem": mem_info(),
        "disks": disks(mounts),
        "gpus": [],
    }
    try:
        payload["gpus"] = gpus()
    except Exception as e:  # nvidia-smi 오류가 나도 나머지는 보냄
        payload["gpu_error"] = str(e)[:300]
    return payload


def send(url, token, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def main():
    url = os.environ.get("LABBOT_URL", "")
    token = os.environ.get("LABBOT_TOKEN", "")
    host = os.environ.get("LABBOT_HOST") or socket.gethostname().split(".")[0]
    mounts = [m.strip() for m in os.environ.get("LABBOT_DISKS", "/").split(",") if m.strip()]
    interval = int(os.environ.get("LABBOT_INTERVAL", "60"))
    once, show = "--once" in sys.argv, "--print" in sys.argv
    if not once and (not url or not token):
        sys.exit("LABBOT_URL, LABBOT_TOKEN 환경변수를 설정해 주세요.")

    cpu = CpuMeter()
    time.sleep(1)
    while True:
        started = time.time()
        payload = collect(cpu, mounts, interval, host)
        if show:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        if url and token:
            try:
                send(url, token, payload)
            except (urllib.error.URLError, OSError) as e:
                print("전송 실패: %s" % e, file=sys.stderr, flush=True)
        if once:
            return
        time.sleep(max(1.0, interval - (time.time() - started)))


if __name__ == "__main__":
    main()
