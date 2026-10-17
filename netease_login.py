#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网易云扫码登录：拿 unikey、出二维码、轮询、把 MUSIC_U 写回配置。

状态码约定（网易云那套）：
  800 二维码过期 / 801 等扫码 / 802 已扫码待确认 / 803 登录成功
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from astrbot.api import logger

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"
BASE = "https://music.163.com"
PLUGIN_NAME = "astrbot_plugin_audio_to_voice"


def _headers(cookie: str = "") -> dict:
    h = {"User-Agent": UA, "Referer": BASE + "/"}
    if cookie:
        h["Cookie"] = cookie
    return h


def _get(url: str, cookie: str = "", timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=_headers(cookie))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def check(cookie: str) -> dict:
    """cookie 还能用吗？返回 {has_cookie, logged_in, nickname, uid}。"""
    cookie = (cookie or "").strip()
    info = {"has_cookie": bool(cookie), "has_music_u": "MUSIC_U=" in cookie,
            "logged_in": False, "nickname": "", "uid": ""}
    if not cookie:
        return info
    try:
        data = json.loads(_get(f"{BASE}/api/nuser/account/get", cookie).decode("utf-8", "ignore") or "{}")
        profile = data.get("profile") or {}
        account = data.get("account") or {}
        info["logged_in"] = bool(profile.get("userId") or account.get("id"))
        info["nickname"] = profile.get("nickname") or ""
        info["uid"] = str(profile.get("userId") or account.get("id") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[DeepseaRadio] 查网易云登录状态失败：{exc}")
    return info


def qr_new() -> tuple[str, str]:
    """要一个二维码：返回 (unikey, 扫进 App 的内容)。"""
    data = json.loads(_get(f"{BASE}/api/login/qrcode/unikey?type=1").decode("utf-8", "ignore") or "{}")
    key = str(data.get("unikey") or "")
    return key, (f"{BASE}/login?codekey={key}" if key else "")


def qr_poll(key: str, cookie: str = "") -> tuple[int, str, str]:
    """轮询一次。803 成功时把 Set-Cookie 拼成 cookie 串返回。"""
    url = f"{BASE}/api/login/qrcode/client/login?key={urllib.parse.quote(key)}&type=1"
    req = urllib.request.Request(url, headers=_headers(cookie))
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        setcookies = resp.headers.get_all("Set-Cookie") or []
    data = json.loads(raw.decode("utf-8", "ignore") or "{}")
    code = int(data.get("code") or -1)
    message = str(data.get("message") or "")
    if code != 803:
        return code, message, ""
    pairs = []
    for item in setcookies:
        head = item.split(";", 1)[0].strip()
        if "=" in head:
            pairs.append(head)
    return 803, "登录成功", "; ".join(pairs)


def qr_png(content: str, path: str) -> str:
    import qrcode  # noqa: PLC0415

    qrcode.make(content).save(path)
    return path


def _config_path() -> str:
    root = os.environ.get("ASTRBOT_ROOT", "/root/AstrBot")
    return os.path.join(root, "data", "config", f"{PLUGIN_NAME}_config.json")


def save_cookie(cookie: str) -> bool:
    path = _config_path()
    try:
        disk: dict = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as fh:
                disk = json.load(fh) or {}
        disk["netease_cookie"] = cookie
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(disk, fh, ensure_ascii=False, indent=2)
        return True
    except (OSError, ValueError) as exc:
        logger.warning(f"[DeepseaRadio] 保存网易云 cookie 失败：{exc}")
        return False
