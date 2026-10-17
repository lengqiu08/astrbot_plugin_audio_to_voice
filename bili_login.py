#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B站登录辅助：状态查询 + 扫码登录。

扫码登录成功后把 cookie 写回插件配置文件（data/config/<插件名>_config.json），
同时更新内存里的 self.config，立刻生效。
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from astrbot.api import logger

BILI_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"
PLUGIN_NAME = "astrbot_plugin_audio_to_voice"


def _get(url: str, cookie: str = "", timeout: int = 15) -> dict:
    headers = {"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore") or "{}")


def cookie_fields(cookie: str) -> list[str]:
    return [p.split("=", 1)[0].strip() for p in (cookie or "").split(";") if "=" in p]


def login_report(cookie: str) -> dict:
    """cookie 有效吗？登录了没？是大会员吗？"""
    cookie = (cookie or "").strip()
    fields = cookie_fields(cookie)
    info = {
        "has_cookie": bool(cookie),
        "has_sessdata": "SESSDATA" in fields,
        "logged_in": False,
        "uname": "",
        "vip": "未知",
        "length": len(cookie),
        "fields": fields[:8],
    }
    if not info["has_sessdata"]:
        return info
    try:
        data = _get("https://api.bilibili.com/x/web-interface/nav", cookie)
        d = data.get("data") or {}
        info["logged_in"] = bool(d.get("isLogin"))
        info["uname"] = d.get("uname") or ""
        status = int(d.get("vipStatus") or 0)
        info["vip"] = "大会员" if status == 1 else "非大会员"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[DeepseaRadio] 查 B站登录状态失败：{exc}")
        info["vip"] = "查不到（网络或接口变动）"
    return info


def qr_new() -> tuple[str, str]:
    data = _get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
    d = data.get("data") or {}
    return str(d.get("qrcode_key") or ""), str(d.get("url") or "")


def qr_poll_once(key: str, cookie: str = "") -> tuple[int, str, str]:
    """轮询一次。返回 (状态码, 说明, cookie)；0 = 登录成功，86038 = 二维码过期。"""
    url = ("https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key="
           + urllib.parse.quote(key))
    headers = {"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        setcookies = resp.headers.get_all("Set-Cookie") or []
    data = json.loads(raw.decode("utf-8", "ignore") or "{}")
    body = data.get("data") or {}
    code = body.get("code")
    msg = body.get("message") or data.get("message") or ""
    if code != 0:
        return int(code or -1), str(msg), ""
    pairs = []
    for item in setcookies:
        head = item.split(";", 1)[0].strip()
        if "=" in head:
            pairs.append(head)
    return 0, "登录成功", "; ".join(pairs)


def qr_png(content: str, path: str) -> str:
    import qrcode  # noqa: PLC0415

    qrcode.make(content).save(path)
    return path


def config_path() -> str:
    root = os.environ.get("ASTRBOT_ROOT", "/root/AstrBot")
    return os.path.join(root, "data", "config", f"{PLUGIN_NAME}_config.json")


def save_cookie(cookie: str) -> bool:
    """写回插件配置文件；失败不致命，内存里那份仍然生效。"""
    path = config_path()
    try:
        disk: dict = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as fh:
                disk = json.load(fh) or {}
        disk["bilibili_cookie"] = cookie
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(disk, fh, ensure_ascii=False, indent=2)
        return True
    except (OSError, ValueError) as exc:
        logger.warning(f"[DeepseaRadio] 保存 B站 cookie 失败：{exc}")
        return False
