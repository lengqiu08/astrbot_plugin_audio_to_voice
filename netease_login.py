#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网易云扫码登录：拿 unikey、出二维码、轮询、把 MUSIC_U 写回配置。

状态码约定（网易云那套）：
  800 二维码过期 / 801 等扫码 / 802 已扫码待确认 / 803 登录成功
"""
from __future__ import annotations

import base64
import json
import os
import random
import string
import urllib.parse
import urllib.request

from Crypto.Cipher import AES

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


def qr_new() -> tuple[str, str, str]:
    """要一个二维码：返回 (unikey, 扫码内容, 服务端给的 cookie)。

    那个 cookie（NMTID 之类）要带着去轮询，不带的话有些账号扫了也一直回 801。
    """
    req = urllib.request.Request(f"{BASE}/api/login/qrcode/unikey?type=1", headers=_headers())
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        setcookies = resp.headers.get_all("Set-Cookie") or []
    data = json.loads(raw.decode("utf-8", "ignore") or "{}")
    key = str(data.get("unikey") or "")
    pairs = [c.split(";", 1)[0].strip() for c in setcookies if "=" in c]
    return key, (f"{BASE}/login?codekey={key}" if key else ""), "; ".join(pairs)


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
    # 网易云把 cookie 放在响应体的 cookie 字段里，不在 Set-Cookie 头；
    # 之前只读头，所以扫了也拿不到 —— 这里优先读 body，头只作兜底。
    body_cookie = str(data.get("cookie") or "").strip()
    if body_cookie:
        return 803, message or "登录成功", body_cookie
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


# ---------------- 手机号登录（weapi 加密） ----------------
_NONCE = b"0CoJUm6Qyw8W8jud"
_IV = b"0102030405060708"
_PUBKEY = "010001"
_MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab1"
    "7a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c9387"
    "0114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047b9"
    "7ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)


def _aes_encrypt(text: bytes, key: bytes) -> bytes:
    pad = 16 - len(text) % 16
    text = text + bytes([pad]) * pad
    return base64.b64encode(AES.new(key, AES.MODE_CBC, _IV).encrypt(text))


def weapi(payload: dict) -> dict:
    """网易云 weapi 加密：AES 两层 + RSA。"""
    text = json.dumps(payload, separators=(",", ":")).encode()
    secret = "".join(random.choices(string.ascii_letters + string.digits, k=16)).encode()
    params = _aes_encrypt(_aes_encrypt(text, _NONCE), secret)
    enc = pow(int.from_bytes(secret[::-1], "big"), int(_PUBKEY, 16), int(_MODULUS, 16))
    return {"params": params.decode(), "encSecKey": format(enc, "x").zfill(256)}


def _post(url: str, data: dict, cookie: str = "", timeout: int = 15) -> tuple[bytes, list[str]]:
    body = urllib.parse.urlencode(data).encode()
    headers = _headers(cookie)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), (resp.headers.get_all("Set-Cookie") or [])


def send_captcha(phone: str) -> tuple[int, str]:
    """给手机号发登录验证码。返回 (code, message)，200 = 已发出。"""
    try:
        raw, _ = _post(f"{BASE}/weapi/sms/captcha/sent?csrf_token=", weapi({"cellphone": phone}))
        data = json.loads(raw.decode("utf-8", "ignore") or "{}")
        return int(data.get("code") or -1), str(data.get("message") or data.get("msg") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[DeepseaRadio] 网易云发验证码失败：{exc}")
        return -1, str(exc)


def login_by_captcha(phone: str, captcha: str) -> tuple[int, str, str]:
    """验证码登录。返回 (code, message, cookie)；200 = 成功。"""
    payload = weapi({"phone": phone, "captcha": captcha, "countrycode": "86", "csrf_token": ""})
    try:
        raw, setcookies = _post(f"{BASE}/weapi/login/cellphone?csrf_token=", payload)
        data = json.loads(raw.decode("utf-8", "ignore") or "{}")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc), ""
    code = int(data.get("code") or -1)
    message = str(data.get("message") or data.get("msg") or "")
    if code != 200:
        return code, message, ""
    body_cookie = str(data.get("cookie") or "").strip()
    if body_cookie:
        return 200, "登录成功", body_cookie
    pairs = []
    for item in setcookies:
        head = item.split(";", 1)[0].strip()
        if "=" in head:
            pairs.append(head)
    return 200, "登录成功", "; ".join(pairs)
