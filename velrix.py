#!/usr/bin/env python3
"""Velrix 自动签到 + 服务器续期脚本

环境变量:
  VELRIX_SESSION  登录后的 velrix_session cookie 值（必填）
  IS_PROXY / PROXY_SERVER   代理配置，由工作流 setup_proxy.sh 写入
  TG_BOT_TOKEN / TG_CHAT_ID   可选，Telegram 通知
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

BJ_TZ = timezone(timedelta(hours=8))
API_BASE = "https://api.velrix.net"
WEB_BASE = "https://www.velrix.net"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"

SESSION = os.environ.get("VELRIX_SESSION", "").strip()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip()
REQUESTS_PROXIES = (
    {"http": PROXY_SERVER, "https": PROXY_SERVER}
    if IS_PROXY and PROXY_SERVER else None
)

_sess = None


def bj_now():
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M")


def http():
    global _sess
    if _sess is None:
        _sess = requests.Session()
        _sess.headers.update({
            "User-Agent": UA,
            "Accept": "application/json",
            "Origin": WEB_BASE,
            "Referer": WEB_BASE + "/",
        })
        _sess.cookies.set("velrix_session", SESSION, domain=".velrix.net", path="/")
        if REQUESTS_PROXIES:
            _sess.proxies.update(REQUESTS_PROXIES)
    return _sess


def api(method, path, **kwargs):
    r = http().request(method, f"{API_BASE}{path}", timeout=30, **kwargs)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


def send_tg(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=30,
        )
        print("📨 TG 消息推送成功")
    except Exception as exc:
        print(f"📨 TG 推送失败: {exc}")


def fetch_csrf():
    try:
        r = requests.get(f"{WEB_BASE}/app/dashboard", headers={"User-Agent": UA}, timeout=30)
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
        if m:
            return m.group(1)
    except Exception as exc:
        print(f"⚠️ 获取 csrf 失败: {exc}")
    return None


def post_with_csrf_fallback(path, **kwargs):
    status, data = api("POST", path, **kwargs)
    if status == 403 and "csrf" in (str(data).lower()):
        token = fetch_csrf()
        if token:
            print("🛡 命中 CSRF 防护，重试中...")
            status, data = api("POST", path, headers={"csrf-token": token}, **kwargs)
    return status, data


def auth_me():
    status, data = api("GET", "/v1/auth/me")
    ok = data.get("success") if isinstance(data, dict) else False
    return status, ok, data.get("data", {}) if ok else {}


def daily_status():
    status, data = api("GET", "/v1/economy/daily")
    return data.get("data") if isinstance(data, dict) else None


def balance_of():
    status, data = api("GET", "/v1/economy/balance")
    if isinstance(data, dict):
        return data.get("data", {}).get("balance")
    return None


def daily_claim():
    status, data = post_with_csrf_fallback("/v1/economy/daily")
    return status, data
    status, data = post_with_csrf_fallback("/v1/economy/daily")
    return status, data


def list_servers():
    status, data = api("GET", "/v1/servers")
    if status != 200 or not (isinstance(data, dict) and data.get("success")):
        return None
    d = data.get("data", {})
    return d.get("servers", []), d.get("slots"), d.get("plan")


def renew_server(server_id):
    status, data = post_with_csrf_fallback(f"/v1/servers/{server_id}/renew")
    return status, data


def main():
    if not SESSION:
        print("❌ 请设置环境变量 VELRIX_SESSION")
        sys.exit(1)

    if PROXY_SERVER:
        print(f"⚙️ 代理已启用：{PROXY_SERVER}")
    else:
        print("🌐 直连模式（未使用代理）")

    print("🌐 验证出口 IP...")
    try:
        ip = http().get("https://api.ipify.org/?format=json", timeout=10).json().get("ip", "")
        ip_masked = re.sub(r'(\d+\.\d+\.)\d+\.\d+', r'\g<1>**.**', ip)
        print(f"📍 出口 IP 确认：{ip_masked}")
    except Exception as exc:
        print(f"⚠️ IP 验证失败: {exc}")

    print(f"🚀 Velrix 任务开始 {bj_now()}")

    status, ok, me = auth_me()
    if not ok:
        msg = (f"🎮 Velrix 任务通知\n⏰ {bj_now()}\n"
               f"❌ 会话无效或已过期 (HTTP {status})\n"
               f"💡 请登录后重新获取 velrix_session")
        print("❌ 会话无效或已过期")
        send_tg(msg)
        sys.exit(1)
    u = me.get("user", {})
    print("✅ 登录有效")

    lines = ["🎮 Velrix 续期通知", f"⏰ 通知时间：{bj_now()}"]

    daily = daily_status() or {}
    gain = None
    if daily.get("claimedToday"):
        sign = "今日已签"
        print("⏳ 今日已签到过")
    else:
        status, data = daily_claim()
        if status == 200 and isinstance(data, dict) and data.get("success"):
            gain = daily.get("nextReward")
            sign = "✅ 成功"
            print("✅ 签到成功")
        else:
            msg = data.get("message") if isinstance(data, dict) else str(data)[:120]
            sign = f"❌ {msg}"
            print(f"⚠️ 签到未成功: {msg}")

    balance = balance_of()
    if balance is not None:
        lines.append(f"💰 余额：{balance}" + (f" (+{gain})" if gain else ""))
        print(f"💰 余额: {balance}")

    time.sleep(1)
    info = list_servers()
    if info is None:
        lines.append("⚠️ 获取服务器列表失败")
    else:
        servers, slots, plan = info
        print(f"📋 服务器数量: {len(servers)}")
        expiry_parts = []
        renew_parts = []
        for srv in servers:
            sid = srv.get("id")
            name = srv.get("name") or sid
            exp = srv.get("expiresAt", "")
            exp_date = exp[:10] if exp else "未知"
            expiry_parts.append(f"{name} {exp_date}")
            print(f"到期 {exp_date}")

            if srv.get("canRenew") is False:
                continue
            status, data = renew_server(sid)
            if status == 200 and isinstance(data, dict) and data.get("success"):
                renew_parts.append(f"✅ {name} 续期成功（-{srv.get('renewCost', '?')} pts）")
                print("✅ 续期成功")
            else:
                msg = data.get("message") if isinstance(data, dict) else str(data)[:120]
                renew_parts.append(f"❌ {name} 续期失败：{msg}")
                print(f"❌ 续期失败: {msg}")
            time.sleep(1.5)

        if servers:
            lines.append(f"📅 利用期限：{' | '.join(expiry_parts)}")

    lines.append(f"📊 签到结果：{sign}")
    if info is not None and renew_parts:
        lines.append(f"📊 续期：{' | '.join(renew_parts)}")

    msg = "\n".join(lines)
    send_tg(msg)
    print("🏁 任务完成")


if __name__ == "__main__":
    main()
