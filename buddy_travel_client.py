# -*- coding: utf-8 -*-
"""WorkBuddy 派猫猫旅行客户端（Buddy Travel）。

接口均来自参考脚本 buddy_station.py（以脚本源码为唯一依据，严禁凭空造）：
  · 旅行接口统一走官网域 www.workbuddy.cn，路径**不带 /v2 前缀**（与成长计划其他接口不同）；
    仅需 Bearer Token，无需 Turing Shield 设备指纹（脚本注释已实测）。
  · GET  /activity/growth/buddy/travel/status  旅行状态（state / arrive_at / server_now / …）
  · GET  /activity/growth/buddy/travel/config  地点配置（locations[].{id,name,duration,reward}）
  · POST /activity/growth/buddy/travel/depart  body {"location_id": N}  派出（写接口）
  · POST /activity/growth/buddy/travel/claim   body {}                  领取（写接口）
  · GET  {domain}/v2/activity/growth/buddy/info  Buddy 信息（name / thumbnail_url / rarity），走登录态域

写接口安全约束（与脚本 travel_auto 一致）：
  · 派遣前必须确认 daily_limit_reached 为假、state 为 idle，达上限绝不发写请求；
  · 领取前必须确认 state 为 arrived（或本地倒计时到点后先刷新状态确认再领）。
"""
import json
import os
import random
import time
import urllib.error
import urllib.request

TRAVEL_DOMAIN = "www.workbuddy.cn"
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"     # GET  旅行状态
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"     # POST 派出 body {"location_id": N}
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"       # POST 领取 body {}
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"     # GET  地点配置
GROWTH_BUDDY_PATH = "/v2/activity/growth/buddy/info"            # GET  Buddy 信息（走登录态域）

# 旅行状态中文映射（与脚本 TRAVEL_STATE_TEXT 一致，不编造）
TRAVEL_STATE_TEXT = {
    "idle": "空闲（可派遣）",
    "traveling": "旅行中",
    "arrived": "已到达（待领取）",
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _auth():
    """复用 acp_credit_client 的凭据读取与兜底扫描。返回 (token, domain)。"""
    try:
        from acp_credit_client import load_credentials
        return load_credentials()
    except Exception:
        return None, None


def _request(domain, path, token, method="GET", payload=None, timeout=12,
             extra_headers=None):
    """通用请求。payload is None 时 GET 不带体 / POST 发 {}。返回 (status, dict)。"""
    url = "https://" + domain + path
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    else:
        body = b"{}" if method == "POST" else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if not any(k.lower() == "user-agent" for k in (extra_headers or {})):
        req.add_header("User-Agent", _UA)
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(300000).decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"code": -1, "msg": raw[:300]}
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return exc.code, {"code": -1, "msg": "HTTP %s" % exc.code}
    except Exception as exc:
        return 0, {"code": -1, "msg": "%s: %s" % (type(exc).__name__, str(exc)[:150])}


def travel_status(token=None, timeout=8):
    """只读：旅行状态。返回 dict：ok / data / error。data 为接口原始 data。"""
    if token is None:
        token, _ = _auth()
    if not token:
        return {"ok": False, "data": None, "error": "未获取到登录凭据（请先登录 WorkBuddy）"}
    st, j = _request(TRAVEL_DOMAIN, TRAVEL_STATUS_PATH, token, method="GET", timeout=timeout)
    if st == 200 and isinstance(j, dict) and j.get("code") == 0:
        return {"ok": True, "data": j.get("data") or {}, "error": None}
    msg = (j.get("msg") if isinstance(j, dict) else None) or ("HTTP %s" % st)
    return {"ok": False, "data": None, "error": msg,
            "http": st, "code": j.get("code") if isinstance(j, dict) else None}


def travel_config(token=None, timeout=8):
    """只读：旅行地点配置。返回 dict：ok / locations(list) / error。"""
    if token is None:
        token, _ = _auth()
    if not token:
        return {"ok": False, "locations": [], "error": "未获取到登录凭据"}
    st, j = _request(TRAVEL_DOMAIN, TRAVEL_CONFIG_PATH, token, method="GET", timeout=timeout)
    if st == 200 and isinstance(j, dict) and j.get("code") == 0:
        d = j.get("data") or {}
        return {"ok": True, "locations": d.get("locations") or [], "error": None}
    return {"ok": False, "locations": [], "error": "HTTP %s" % st}


def travel_depart(token=None, location_id=None, config=None, timeout=12):
    """写接口：派出 Buddy 旅行。location_id 为空时从地点列表中随机。

    调用方必须已确认「未达每日上限且 state 为 idle」；本函数不重复查询。
    返回 dict：action/success/location_id/message/arrive_at。
    """
    if token is None:
        token, _ = _auth()
    if not token:
        return {"action": "failed", "success": False, "message": "未获取到登录凭据"}
    chosen = location_id
    if chosen is None:
        ids = [loc.get("id") for loc in (config or []) if loc.get("id") is not None]
        if not ids:
            return {"action": "failed", "success": False, "message": "未能获取可选地点列表，已跳过派遣"}
        chosen = random.choice(ids)
    st, body = _request(TRAVEL_DOMAIN, TRAVEL_DEPART_PATH, token,
                        method="POST", payload={"location_id": chosen}, timeout=timeout)
    if st == 200 and isinstance(body, dict) and body.get("code") == 0:
        data = body.get("data") or {}
        loc = data.get("location") or {}
        if not loc:
            # 部分响应 location 为空时，回读状态补地点名
            st2 = travel_status(token)
            if st2.get("ok") and isinstance(st2["data"], dict):
                l2 = st2["data"].get("location") or {}
                loc = l2 if isinstance(l2, dict) else {}
        return {"action": "departed", "success": True, "location_id": chosen,
                "location_name": loc.get("name"),
                "arrive_at": data.get("arrive_at"),
                "message": "已派出 Buddy 前往【%s】" % (loc.get("name") or ("地点 %s" % chosen))}
    return {"action": "failed", "success": False, "location_id": chosen,
            "message": (body.get("msg") if isinstance(body, dict) else None) or ("HTTP %s" % st),
            "code": body.get("code") if isinstance(body, dict) else None}


def travel_claim(token=None, timeout=12):
    """写接口：领取已到达旅行的积分。返回 dict：action/success/reward/message。"""
    if token is None:
        token, _ = _auth()
    if not token:
        return {"action": "failed", "success": False, "message": "未获取到登录凭据"}
    st, body = _request(TRAVEL_DOMAIN, TRAVEL_CLAIM_PATH, token,
                        method="POST", payload={}, timeout=timeout)
    if st == 200 and isinstance(body, dict) and body.get("code") == 0:
        data = body.get("data") or {}
        credit = data.get("reward_credit")
        return {"action": "claimed", "success": True, "reward_credit": credit,
                "message": "已领取旅行奖励 +%s 积分" % (credit if credit is not None else "?")}
    return {"action": "failed", "success": False,
            "message": (body.get("msg") if isinstance(body, dict) else None) or ("HTTP %s" % st),
            "code": body.get("code") if isinstance(body, dict) else None}


def buddy_info(token=None, timeout=8):
    """只读：Buddy 信息。走登录态域 {domain}/v2/activity/growth/buddy/info。
    返回 dict：ok / buddy(原始 dict 或 None) / error。"""
    if token is None:
        token, domain = _auth()
    else:
        _tok, domain = _auth()
    if not token:
        return {"ok": False, "buddy": None, "error": "未获取到登录凭据（请先登录 WorkBuddy）"}
    domain = domain or "www.codebuddy.cn"
    st, j = _request(domain, GROWTH_BUDDY_PATH, token, method="GET", timeout=timeout)
    if st == 200 and isinstance(j, dict) and j.get("code") == 0:
        d = j.get("data") or {}
        return {"ok": True, "buddy": d.get("buddy") or {}, "error": None}
    msg = (j.get("msg") if isinstance(j, dict) else None) or ("HTTP %s" % st)
    return {"ok": False, "buddy": None, "error": msg,
            "http": st, "code": j.get("code") if isinstance(j, dict) else None}


def download_thumb(url, cache_path, timeout=15):
    """下载 Buddy 缩略图（PNG）到本地缓存。返回本地路径或 None。

    纯标准库 urllib，带 UA；失败静默返回 None，不抛异常。
    """
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", _UA)
        req.add_header("Referer", "https://www.workbuddy.cn/")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(400000)
        if not raw:
            return None
        tmp = cache_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, cache_path)
        return cache_path
    except Exception:
        try:
            if os.path.exists(cache_path + ".tmp"):
                os.remove(cache_path + ".tmp")
        except Exception:
            pass
        return None
