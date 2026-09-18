# -*- coding: utf-8 -*-
"""WorkBuddy 积分查询客户端（严格只读）

用途：给桌面挂件提供「一键刷新 / 定时取当前积分余额」的能力。

两条取数路径：
  1) direct（默认，毫秒级）：读取本机 WorkBuddy 登录态文件里的 accessToken，
     直接请求 POST https://<domain>/v2/billing/meter/checkin-activity-status。
  2) acp（兜底，慢，约 1-4 分钟）：通过本机 ACP 通道驱动 WorkBuddy 跑一轮只读查询，
     再用正则从其回复文本中解析数值。仅在 direct 不可用时开启 use_acp_fallback=True。

两种积分口径（数值不同，别混用）：
  - query_credits / query_credits_detail → 签到活动累计领取积分（整数，如 300）。
  - query_balance → App 首屏「积分余额」（两位小数，如 6434.52），
    来自 POST {domain}/billing/meter/get-user-resource-summary（注意：网关路由**不带** /v2），
    取 data.Packages[].CycleRemainCapacity **无条件求和**（服务端已做去重与过滤）。
    该接口必须带浏览器三件套头（User-Agent / Referer / Origin），否则返回 403。

安全约束：
  - 只发只读查询请求，绝不调用签到 / 领取奖励 / 任何写接口。
  - token 只用于内存中的 Authorization 头，绝不落盘、绝不打印。
  - 全程超时与异常兜底，query_credits() 永远不抛异常。

依赖：纯标准库（urllib / json / re / subprocess），不需要 requests。
实测（Windows 11）：balance=300 streak=3 checked_in=True，耗时约 0.3s。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

NL2 = chr(10) + chr(10)
DKEY = "da" + "ta: "
ENDPOINT_PATH = "/v2/billing/meter/checkin-activity-status"
# 余额接口的网关路由不带 /v2，带 /v2 会 404
BALANCE_PATH = "/billing/meter/get-user-resource-summary"
AUTH_FILENAME = "workbuddy-desktop.info"


def _auth_candidates():
    home = os.path.expanduser("~")
    la = os.environ.get("LOCALAPPDATA", "")
    ra = os.environ.get("APPDATA", "")
    c = []
    c.append(os.path.join(la, "CodeBuddyExtension", "Data", "Public", "auth", AUTH_FILENAME))
    c.append(os.path.join(ra, "CodeBuddyExtension", "Data", "Public", "auth", AUTH_FILENAME))
    if sys.platform == "darwin":
        c.append(os.path.join(home, "Library", "Application Support", "CodeBuddyExtension",
                              "Data", "Public", "auth", AUTH_FILENAME))
    if sys.platform.startswith("linux"):
        c.append(os.path.join(home, ".config", "CodeBuddyExtension",
                              "Data", "Public", "auth", AUTH_FILENAME))
        c.append(os.path.join(home, ".local", "share", "CodeBuddyExtension",
                              "Data", "Public", "auth", AUTH_FILENAME))
    c.append(os.path.join(home, ".workbuddy", "auth", AUTH_FILENAME))
    return [p for p in c if p and os.path.isabs(p)]


def _scan_auth_file(max_seconds=6.0):
    t0 = time.time()
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        roots = [v for v in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")) if v]
    elif sys.platform == "darwin":
        roots = [os.path.join(home, "Library", "Application Support", "CodeBuddyExtension")]
    else:
        roots = [os.path.join(home, ".config", "CodeBuddyExtension"),
                 os.path.join(home, ".local", "share", "CodeBuddyExtension")]
    roots.append(os.path.join(home, ".workbuddy"))
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if time.time() - t0 > max_seconds:
                return None
            if AUTH_FILENAME in filenames:
                return os.path.join(dirpath, AUTH_FILENAME)
    return None


def find_auth_file():
    for p in _auth_candidates():
        if os.path.isfile(p):
            return p
    return _scan_auth_file()


def load_credentials():
    """返回 (token, domain)；失败返回 (None, None)。token 不落盘、不打印。"""
    path = find_auth_file()
    if not path:
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        auth = d.get("auth") or d
        tok = auth.get("accessToken")
        dom = auth.get("domain") or "www.codebuddy.cn"
        if not tok:
            return None, None
        return tok, str(dom).strip()
    except Exception:
        return None, None


def _post_json(url, body=b"{}", headers=None, timeout=8):
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(200000).decode("utf-8", "replace")


def _discover_acp_ports_win():
    """Windows：netstat -ano + tasklist 匹配 WorkBuddy/CodeBuddy 进程。"""
    found = []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                             text=True, timeout=12, errors="replace").stdout
    except Exception:
        return found
    cand = []
    for line in out.splitlines():
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        addr, pid = parts[1], parts[-1]
        if not addr.startswith("127.0.0.1"):
            continue
        try:
            port = int(addr.rsplit(":", 1)[1])
        except Exception:
            continue
        if port in (18488, 18489, 18490):
            continue
        cand.append((port, pid))
    if not cand:
        return found
    try:
        tl = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                            text=True, timeout=15, errors="replace").stdout
    except Exception:
        return found
    wb = set()
    for line in tl.splitlines():
        low = line.lower()
        if "workbuddy" in low or "codebuddy" in low:
            m = re.search(r'"[^"]+","(\d+)"', line)
            if m:
                wb.add(m.group(1))
    for port, pid in cand:
        if pid in wb:
            found.append(port)
    return sorted(set(found))


def _discover_acp_ports_posix():
    """macOS/Linux：lsof 列出本机监听端口 + ps 匹配进程名，返回候选 ACP 端口。

    兼容缺省场景：lsof / ps 不可用或未启动 WorkBuddy 时返回空，调用方走直连兜底。
    """
    found = []
    try:
        out = subprocess.run(["lsof", "-iTCP", "-sTCP:LISTEN", "-n", "-P"],
                             capture_output=True, text=True, timeout=12,
                             errors="replace").stdout
    except Exception:
        return found
    cand = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 9:
            continue
        # NETWORK 行形如: python 12345 user 9u IPv4 0x... 0t0 TCP 127.0.0.1:18491 (LISTEN)
        proto = parts[7] if len(parts) > 7 else ""
        if not proto.startswith("TCP"):
            continue
        addr = parts[8]
        if not addr.startswith("127.0.0.1:"):
            continue
        try:
            port = int(addr.rsplit(":", 1)[1])
        except Exception:
            continue
        if port in (18488, 18489, 18490):
            continue
        cand.append((port, parts[1]))
    if not cand:
        return found
    try:
        ps = subprocess.run(["ps", "-eo", "pid=,comm="], capture_output=True,
                            text=True, timeout=10, errors="replace").stdout
    except Exception:
        return found
    wb = set()
    for line in ps.splitlines():
        low = line.lower()
        if "workbuddy" in low or "codebuddy" in low:
            try:
                wb.add(line.split(None, 1)[0].strip())
            except Exception:
                pass
    for port, pid in cand:
        if pid in wb:
            found.append(port)
    return sorted(set(found))


def discover_acp_ports():
    """发现本机 WorkBuddy 的 ACP 端口（排除 18488/18489/18490 固定端口）。

    跨平台：Windows 走 netstat+tasklist；macOS/Linux 走 lsof+ps。
    """
    if sys.platform.startswith("win"):
        return _discover_acp_ports_win()
    return _discover_acp_ports_posix()


QUESTION = ("[只读积分查询，禁止一切写操作] 请查询当前账号 WorkBuddy 积分余额。"
            "唯一任务：读取积分余额与连续天数，用最省步骤的方式取得数值。"
            "严格禁止：禁止调用任何 Skill；禁止使用 AskUserQuestion 提问；"
            "禁止创建或修改任何文件；禁止执行签到；禁止领取任何奖励；禁止任何写操作。"
            "最后只输出三行纯文本，不要任何解释：\n"
            "积分余额=<纯数字>\n连续天数=<纯数字>\n今日已记录=<是或否>")

RE_BALANCE = re.compile(r"积分余额\s*[=:：]\s*(\d+)")
RE_STREAK = re.compile(r"连续(?:登录)?天数\s*[=:：]\s*(\d+)")
RE_CHECKED = re.compile(r"今日已记录\s*[=:：]\s*([是否])")


def _acp_query(timeout=150):
    """通过 ACP 驱动 WorkBuddy 跑一轮只读查询，返回 (text, note)。"""
    for port in discover_acp_ports()[:3]:
        try:
            _st, body = _post_json("http://127.0.0.1:%d/api/v1/acp/connect" % port,
                                   json.dumps({"clientInfo": {"name": "wb-widget", "version": "1.0"}}).encode(),
                                   timeout=8)
            c = json.loads(body)
            cid, stok = c["connectionId"], c.get("sessionToken", "")
            h = {"acp-connection-id": cid, "acp-session-token": stok,
                 "x-codebuddy-request": "1", "Accept": "application/json, text/event-stream"}
            _post_json("http://127.0.0.1:%d/api/v1/acp" % port,
                       json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": 1, "capabilities": {},
                                              "clientInfo": {"name": "wb-widget", "version": "1.0"}}}).encode(),
                       headers=h, timeout=15)
            _st2, b2 = _post_json("http://127.0.0.1:%d/api/v1/acp" % port,
                                  json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/new",
                                              "params": {"cwd": os.getcwd(), "mcpServers": []}}).encode(),
                                  headers=h, timeout=30)
            m = re.search(r'"sessionId":"([0-9a-fA-F-]{36})"', b2)
            if not m:
                continue
            sid = m.group(1)
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/v1/acp" % port,
                data=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                                 "params": {"sessionId": sid,
                                            "prompt": [{"type": "text", "text": QUESTION}]}}).encode(),
                headers=dict(h, **{"Content-Type": "application/json"}), method="POST")
            buf, texts, t0 = "", [], time.time()
            with urllib.request.urlopen(req, timeout=timeout) as r:
                while time.time() - t0 < timeout:
                    chunk = r.read(4096)
                    if not chunk:
                        break
                    buf += chunk.decode("utf-8", "replace")
                    while NL2 in buf:
                        frame, buf = buf.split(NL2, 1)
                        if DKEY not in frame:
                            continue
                        try:
                            ev = json.loads(frame.split(DKEY, 1)[1])
                        except Exception:
                            continue
                        up = (ev.get("params") or {}).get("update") or {}
                        if up.get("sessionUpdate") == "agent_message_chunk":
                            t = (up.get("content") or {}).get("text")
                            if t:
                                texts.append(t)
                        if "result" in ev or "error" in ev:
                            buf = ""
                            break
                    if not buf and texts:
                        break
            full = "".join(texts)
            if full:
                return full, "acp port=%d" % port
        except Exception:
            continue
    return "", "acp unavailable"


def _blank(error=None):
    return {"ok": False, "balance": None, "streak": None, "checked_in": None,
            "raw_text": "", "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "none", "error": error}


def query_credits(timeout=8, use_acp_fallback=False, acp_timeout=150):
    """查询 WorkBuddy 积分（只读）。

    返回 dict：ok / balance(int) / streak(int) / checked_in(bool) /
               raw_text(str) / updated_at(str) / source / error
    永不抛异常；失败时数值字段为 None，error 写明原因。
    """
    try:
        tok, dom = load_credentials()
        if tok:
            try:
                _st, raw = _post_json("https://%s%s" % (dom, ENDPOINT_PATH), b"{}",
                                      headers={"Authorization": "Bearer " + tok}, timeout=timeout)
                j = json.loads(raw)
                data = (j.get("data") or {}) if isinstance(j, dict) else {}
                if isinstance(j, dict) and data and not j.get("code"):
                    today = datetime.now().strftime("%Y-%m-%d")
                    checked = bool(data.get("today_checked_in"))
                    if not checked:
                        checked = today in (data.get("checkin_dates") or [])
                    bal = int(data.get("total_credits"))
                    streak = int(data.get("streak_days") or 0)
                    return {"ok": True, "balance": bal, "streak": streak, "checked_in": checked,
                            "raw_text": "积分余额=%d 连续天数=%d 今日已记录=%s"
                                        % (bal, streak, "是" if checked else "否"),
                            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "source": "direct", "error": None}
                return _blank("响应不符合预期: code=%s"
                              % (j.get("code") if isinstance(j, dict) else "?"))
            except urllib.error.HTTPError as e:
                err = ("token 失效或未授权 (HTTP %s)" % e.code) if e.code in (401, 403) else ("HTTP %s" % e.code)
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, str(e)[:120])
        else:
            err = "未找到 WorkBuddy 登录态文件（可能未登录）"

        if use_acp_fallback:
            text, note = _acp_query(timeout=acp_timeout)
            if text:
                mb, ms, mc = RE_BALANCE.search(text), RE_STREAK.search(text), RE_CHECKED.search(text)
                if mb:
                    return {"ok": True,
                            "balance": int(mb.group(1)),
                            "streak": int(ms.group(1)) if ms else None,
                            "checked_in": (mc.group(1) == "是") if mc else None,
                            "raw_text": text[-400:],
                            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "source": note, "error": None}
                return _blank("ACP 回复中未解析到积分数值")
            return _blank("直连失败(%s)；ACP 兜底也不可用" % err)
        return _blank(err)
    except Exception as e:
        return _blank("%s: %s" % (type(e).__name__, str(e)[:150]))


def _parse_dt(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def query_credits_detail(timeout=8):
    """查询 WorkBuddy 积分的完整详情（只读，永不抛异常）。

    在 query_credits 的基础上额外返回：本月记录天数、累计记录天数、本周进度、
    活动名称/主题/期数、活动周期、活动剩余天数、全勤预计可再得积分。
    失败时 ok=False，error 写明原因。
    """
    r = {"ok": False, "error": None,
         "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        tok, dom = load_credentials()
        if not tok:
            r["error"] = "未找到 WorkBuddy 登录态文件（可能未登录）"
            return r
        _st, raw = _post_json("https://%s%s" % (dom, ENDPOINT_PATH), b"{}",
                              headers={"Authorization": "Bearer " + tok}, timeout=timeout)
        j = json.loads(raw)
        d = (j.get("data") or {}) if isinstance(j, dict) else {}
        if not d or (isinstance(j, dict) and j.get("code")):
            r["error"] = "响应不符合预期: code=%s" % (j.get("code") if isinstance(j, dict) else "?")
            return r

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        dates = [x for x in (d.get("checkin_dates") or []) if isinstance(x, str)]
        month_days = sum(1 for x in dates if x.startswith(now.strftime("%Y-%m")))
        checked = bool(d.get("today_checked_in")) or (today in dates)
        week_progress = [bool(v) for v in (d.get("week_progress") or [])]
        week_days = int(d.get("week_checkin_days") or sum(1 for v in week_progress if v))
        end = _parse_dt(d.get("end_time") or "")
        remain_days = max(0, (end.date() - now.date()).days) if end else None
        daily = int(d.get("daily_credit") or d.get("today_credit") or 0)

        r.update({
            "ok": True,
            "balance": int(d.get("total_credits") or 0),
            "streak": int(d.get("streak_days") or 0),
            "checked_in": checked,
            "month_days": month_days,
            "total_days": len(dates),
            "week_days": week_days,
            "week_total": 7,
            "week_progress": week_progress,
            "activity_name": d.get("activity_name") or "",
            "theme_name": d.get("theme_name") or "",
            "season": d.get("season"),
            "active": bool(d.get("active")),
            "start_time": d.get("start_time") or "",
            "end_time": d.get("end_time") or "",
            "remain_days": remain_days,
            "daily_credit": daily,
            "forecast_credit": (remain_days * daily) if (remain_days is not None and daily) else None,
            "checkin_dates": dates,
        })
        return r
    except urllib.error.HTTPError as e:
        r["error"] = ("token 失效或未授权 (HTTP %s)" % e.code) if e.code in (401, 403) else ("HTTP %s" % e.code)
    except Exception as e:
        r["error"] = "%s: %s" % (type(e).__name__, str(e)[:150])
    return r


def _balance_headers(tok, dom):
    """余额接口所需的头：Bearer + 浏览器三件套（缺 Referer/Origin/UA 会 403）。"""
    return {
        "Authorization": "Bearer " + tok,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": "zh",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CodeBuddyExtension/1.0",
        "Referer": "https://%s/" % dom,
        "Origin": "https://%s" % dom,
    }


def query_balance(timeout=10):
    """查询 WorkBuddy 首屏「积分余额」（只读，永不抛异常）。

    即 App 里显示的带两位小数的额度（如 6434.52），口径为
    Σ data.Packages[].CycleRemainCapacity。

    返回 dict：
      ok / balance(float) / display(str, 千位分隔+最多两位小数) /
      total_capacity(float) / used_capacity(float) / packages(list) /
      is_paid_user(bool) / updated_at / error
    失败时 ok=False，error 写明原因（含 401/403 的 token 失效提示）。
    """
    r = {"ok": False, "balance": None, "display": None,
         "total_capacity": None, "used_capacity": None, "packages": [],
         "is_paid_user": None, "error": None,
         "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        tok, dom = load_credentials()
        if not tok:
            r["error"] = "未找到 WorkBuddy 登录态文件（可能未登录）"
            return r
        _st, raw = _post_json("https://%s%s" % (dom, BALANCE_PATH), b"{}",
                              headers=_balance_headers(tok, dom), timeout=timeout)
        j = json.loads(raw)
        d = (j.get("data") or {}) if isinstance(j, dict) else {}
        if not d or (isinstance(j, dict) and j.get("code")):
            r["error"] = "响应不符合预期: code=%s" % (j.get("code") if isinstance(j, dict) else "?")
            return r

        left = total = used = 0.0
        pkgs = []
        for pk in (d.get("Packages") or []):
            try:
                t = float(pk.get("CycleTotalCapacity") or 0)
                rem = float(pk.get("CycleRemainCapacity") or 0)
                u = float(pk.get("CycleUsedCapacity") or 0)
            except (TypeError, ValueError):
                continue
            left += rem
            total += t
            used += u
            pkgs.append({"code": pk.get("PackageCode"), "total": t,
                         "remain": rem, "used": u})
        r.update({
            "ok": True,
            "balance": left,
            "display": "{:,.2f}".format(left).rstrip("0").rstrip("."),
            "total_capacity": total,
            "used_capacity": used,
            "packages": pkgs,
            "is_paid_user": bool(d.get("IsPaidUser")),
        })
        return r
    except urllib.error.HTTPError as e:
        r["error"] = ("token 失效或未授权 (HTTP %s)" % e.code) if e.code in (401, 403) else ("HTTP %s" % e.code)
    except Exception as e:
        r["error"] = "%s: %s" % (type(e).__name__, str(e)[:150])
    return r


if __name__ == "__main__":
    t0 = time.time()
    b = query_balance()
    if b["ok"]:
        print("积分余额(额度) = %s" % b["display"])
        print("总额度/已用     = %.2f / %.2f" % (b["total_capacity"], b["used_capacity"]))
        for pk in b["packages"]:
            print("  - %-32s remain=%.2f / total=%.2f" % (pk["code"], pk["remain"], pk["total"]))
    else:
        print("积分余额(额度) = 失败: %s" % b["error"])
    print("balance_elapsed = %.2fs" % (time.time() - t0))
    print("-" * 40)
    t1 = time.time()
    r = query_credits()
    for k in ("ok", "balance", "streak", "checked_in", "raw_text", "updated_at", "source", "error"):
        print("%-10s = %s" % (k, r[k]))
    print("checkin_elapsed = %.2fs" % (time.time() - t1))

