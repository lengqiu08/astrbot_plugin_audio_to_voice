#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深海电台 —— 小深音乐台重启版。

一条指令做完：搜歌 → 出歌单 → 回编号 → 发语音条（或原文件）。
外加：本地曲库、收歌入库、音频↔语音条双向转换。

设计原则：
  · 只保留真用得上的动作，不做 WebUI 面板、不做扫码登录、不做家政；
  · 搜索三源合并（本地曲库 / B站音乐区 / 网易云），谁有就给谁；
  · 平台差异（QQ 官方接口 300 秒硬限）在发送层统一处理，上层不关心。

为什么发语音要转 wav 而不是直接 mp3：官方接口会糟蹋音频；
为什么切片 299.5 秒：300.0 能过、302.0 被拒，贴着切会被容器多出的几十毫秒顶穿。
"""
from __future__ import annotations

import asyncio
import glob
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain, Record, Reply
from astrbot.api.message_components import BaseMessageComponent, ComponentType
from astrbot.api.star import Context, Star

try:
    import pilk  # silk 编解码，只有 codec=silk 时才用得上
except ImportError:  # pragma: no cover
    pilk = None

try:
    from . import bili_login
except ImportError:  # pragma: no cover
    import bili_login

try:
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # pragma: no cover
    error_response = json_response = request = None

try:
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.core.platform.message_session import MessageSesion
except ImportError:  # pragma: no cover
    MessageChain = MessageSesion = None

class RawJson(BaseMessageComponent):
    """OneBot json 段的正确形状：data.data 是 JSON 字符串，不是对象。"""

    type: ComponentType = ComponentType.Json
    payload: str = ""

    def toDict(self):
        return {"type": "json", "data": {"data": self.payload}}


RATE = 24000
AUDIO_EXT = (".flac", ".mp3", ".m4a", ".wav", ".ogg", ".aac", ".opus", ".silk")
OFFICIAL_HARD_MAX = 300.0     # QQ 官方接口硬限
OFFICIAL_SLICE = 299.5        # 留 0.5 秒给容器封装误差
OFFICIAL_PLATFORMS = ("qq_official", "qqofficial")
LOOP_HINTS = ("循环", "歌单", "合集", "串烧", "一小时", "1小时", "车载", "联播")
MAX_ONLINE_SECONDS = 15 * 60  # 超过这个时长的投稿当合集，不进歌单
BILI_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"
NETEASE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"
_PLUGIN_NAME = "astrbot_plugin_audio_to_voice"


# ---------------- 小工具 ----------------
def _probe(path: str) -> float:
    """ffprobe 取时长（秒），失败给 0。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _mmss(secs: float) -> str:
    secs = max(0, int(secs or 0))
    return f"{secs // 60}:{secs % 60:02d}"


def _norm_reply(raw: str) -> str:
    text = (raw or "").strip().strip(" \t\r\n「」『』【】《》\"'“”‘’")
    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    return " ".join(text.split())


def _mmss_to_secs(text: str) -> int:
    parts = [int(x) for x in re.findall(r"\d+", text or "")][:3]
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def _strip_uri(path: str) -> str:
    return path[7:] if path.startswith("file://") else path


def _local_audio_of(component) -> str:
    for attr in ("path", "file_"):
        value = getattr(component, attr, None)
        if isinstance(value, str) and value:
            value = _strip_uri(value)
            if os.path.exists(value):
                return os.path.abspath(value)
    return ""


class DeepseaRadio(Star):
    """深海电台：点歌 + 本地曲库 + 双向转换。"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self._session_file = os.path.join(self.plugin_dir, "sessions.json")
        self._sessions: dict[str, dict] = self._load_sessions()
        self._cooldowns: dict[str, float] = {}
        self._duration_cache: dict[str, float] = {}
        self._recent_play: dict[str, tuple[str, float]] = {}
        self._last_clean = 0.0
        self._plays_file = os.path.join(self.plugin_dir, "plays.json")
        self._plays: dict = self._load_plays()
        self._bili_buvid = ""
        self._register_page_api()

    # ---------------- 会话持久化 ----------------
    # 插件重载（我改代码、你点重载）会重建实例，内存里的歌单就没了。
    # 落盘一份，重载后回编号还能接着选；过期的顺手扔掉。
    def _load_sessions(self) -> dict:
        try:
            with open(self._session_file, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return {}
        ttl = float(self._cfg("session_ttl", 600) or 600)
        now = time.time()
        return {
            k: v for k, v in raw.items()
            if isinstance(v, dict) and now - float(v.get("created", 0)) <= ttl
        }

    def _save_sessions(self) -> None:
        try:
            with open(self._session_file, "w", encoding="utf-8") as fh:
                json.dump(self._sessions, fh, ensure_ascii=False)
        except OSError as exc:
            logger.warning(f"[DeepseaRadio] 会话落盘失败：{exc}")

    # ---------------- 配置 ----------------
    def _cfg(self, key: str, default):
        value = self.config.get(key, default)
        return default if value in (None, "") else value

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enable", True))

    def _library_dirs(self) -> list[str]:
        raw = str(self._cfg("library_dirs", "") or "")
        return [p.strip() for p in raw.replace("；", ";").split(";") if p.strip()]

    def _library_root(self) -> str:
        dirs = self._library_dirs()
        if dirs:
            os.makedirs(dirs[0], exist_ok=True)
            return dirs[0]
        return tempfile.gettempdir()

    def _is_official(self, event: AstrMessageEvent) -> bool:
        mode = str(self._cfg("platform_mode", "auto") or "auto").lower()
        if mode in ("official", "foofficial"):
            return True
        if mode in ("napcat", "aiocqhttp", "protocol"):
            return False
        try:
            name = str(event.get_platform_name() or "").lower()
        except Exception:  # noqa: BLE001
            name = ""
        return name in OFFICIAL_PLATFORMS or "official" in name

    # ---------------- 音频转换 ----------------
    def _to_wav(self, src: str, start: float | None = None, dur: float | None = None) -> str:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("缺 ffmpeg")
        out = os.path.join(tempfile.gettempdir(), f"dsr_{os.getpid()}_{os.urandom(3).hex()}.wav")
        work, cleanup = src, []
        if src.lower().endswith(".silk"):
            if pilk is None:
                raise RuntimeError("缺 pilk，装一下才能解 silk")
            pcm = out + ".pcm"
            if not pilk.decode(src, pcm):
                raise RuntimeError("silk 解码失败")
            work, cleanup = pcm, [pcm]
        cmd = [ffmpeg, "-y", "-loglevel", "error"]
        if start:
            cmd += ["-ss", str(start)]
        if dur:
            cmd += ["-t", str(dur)]
        if work is not src:
            cmd += ["-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", work]
        else:
            cmd += ["-i", work]
        cmd += ["-ar", str(RATE), "-ac", "1", out]
        try:
            subprocess.run(cmd, check=True, timeout=600)
        finally:
            for path in cleanup:
                if os.path.exists(path):
                    os.remove(path)
        return out

    def _to_silk(self, src: str, start: float | None = None, dur: float | None = None) -> str:
        if pilk is None:
            raise RuntimeError("缺 pilk，先 pip install pilk")
        wav = self._to_wav(src, start, dur)
        out = os.path.join(tempfile.gettempdir(), f"dsr_{os.getpid()}_{os.urandom(3).hex()}.silk")
        try:
            pcm = out + ".pcm"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", wav,
                 "-f", "s16le", "-ar", str(RATE), "-ac", "1", pcm], check=True, timeout=600)
            if not pilk.encode(pcm, out, RATE):
                raise RuntimeError("silk 编码失败")
        finally:
            for path in (wav, out + ".pcm"):
                if os.path.exists(path):
                    os.remove(path)
        return out

    def _to_mp3(self, src: str, bitrate: str = "192k") -> str:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("缺 ffmpeg")
        out = os.path.join(tempfile.gettempdir(), f"dsr_{os.getpid()}_{os.urandom(3).hex()}.mp3")
        work, cleanup = src, []
        if src.lower().endswith(".silk"):
            if pilk is None:
                raise RuntimeError("缺 pilk，装一下才能解 silk")
            pcm = out + ".pcm"
            if not pilk.decode(src, pcm):
                raise RuntimeError("silk 解码失败")
            work, cleanup = pcm, [pcm]
        cmd = [ffmpeg, "-y", "-loglevel", "error"]
        if work is not src:
            cmd += ["-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", work]
        else:
            cmd += ["-i", work]
        cmd += ["-b:a", bitrate, "-ar", "44100", "-ac", "2", out]
        try:
            subprocess.run(cmd, check=True, timeout=600)
        finally:
            for path in cleanup:
                if os.path.exists(path):
                    os.remove(path)
        return out

    def _pieces(self, src: str, total: float, limit: int, official: bool) -> list[str]:
        """按需切段。codec=wav 交给适配器编 silk（兼容性最好），silk 才自己编。"""
        codec = str(self._cfg("codec", "wav") or "wav").lower()
        make = self._to_silk if codec == "silk" else self._to_wav
        if not limit or total <= limit:
            return [make(src)]
        count = max(1, int(-(-total // limit)))
        seg = total / count
        pieces, start = [], 0.0
        while start < total - 0.5:
            dur = min(seg, total - start)
            path = make(src, start=start, dur=dur)
            if official:
                real = _probe(path)
                if real > OFFICIAL_HARD_MAX:
                    os.remove(path)
                    cut = max(1.0, dur - (real - OFFICIAL_SLICE))
                    logger.warning(f"[DeepseaRadio] 切出 {real:.2f}s 超硬限，重切成 {cut:.2f}s")
                    path = make(src, start=start, dur=cut)
            pieces.append(path)
            start += dur
        return pieces

    def _plan(self, event: AstrMessageEvent, total: float) -> tuple[str, int]:
        """('voice', 0) / ('segments', 段长) / ('file', 0)"""
        mode = str(self._cfg("mode", "auto") or "auto").lower()
        official = self._is_official(event)
        limit = OFFICIAL_SLICE if official else float(self._cfg("voice_max_seconds", 900) or 0)
        if mode == "file":
            return "file", 0
        if limit <= 0:          # 0 = 不切，整首一条（协议端可以这么玩）
            return "voice", 0
        if total > limit:
            count = int(-(-total // limit))
            if count > int(self._cfg("max_segments", 6) or 6):
                return "file", 0
            return "segments", int(limit)
        return "voice", 0

    async def _find_media_in(self, event: AstrMessageEvent) -> str:
        """从当前消息或它引用的消息里挖出本地音频。

        AstrBot 新版里 `File.file` 是同步下载的 property，在异步上下文里取会告警且拿不到
        路径（日志里那条「不可以在异步上下文中同步等待下载!」就是它）。所以先看 path/file_，
        再退回 `await get_file()`，最后才自己按 url 下载。
        """
        comps = getattr(getattr(event, "message_obj", None), "message", None) or []
        for comp in comps:
            if isinstance(comp, (Record, File)):
                path = await self._resolve_audio(comp)
                if path:
                    return path
            chain = getattr(comp, "chain", None) if isinstance(comp, Reply) else None
            for sub in chain or []:
                if isinstance(sub, (Record, File)):
                    path = await self._resolve_audio(sub)
                    if path:
                        return path
        return ""

    async def _resolve_audio(self, comp) -> str:
        for attr in ("path", "file_"):
            value = getattr(comp, attr, None)
            if isinstance(value, str) and value:
                path = _strip_uri(value)
                if os.path.exists(path):
                    return os.path.abspath(path)
        url = getattr(comp, "url", "") or ""
        if isinstance(url, str) and url.startswith("file://"):
            path = _strip_uri(url)
            if os.path.exists(path):
                return os.path.abspath(path)
        getter = getattr(comp, "get_file", None)
        if callable(getter):
            try:
                got = await getter()
                if isinstance(got, str) and got:
                    path = _strip_uri(got)
                    if os.path.exists(path):
                        return os.path.abspath(path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[DeepseaRadio] get_file 失败：{exc}")
        if isinstance(url, str) and url.startswith("http"):
                try:
                    suffix = ".silk" if ".silk" in url.lower() else ".mp3"
                    return await asyncio.to_thread(self._download, url, suffix)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[DeepseaRadio] 拉取音频失败：{exc}")
        return ""

    @staticmethod
    def _safe_name(name: str, limit: int = 60) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", name or "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return (cleaned[:limit].rstrip(" .") or "song")

    @staticmethod
    def _real_ext(path: str) -> str:
        try:
            fmt = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, timeout=10).stdout.strip().lower()
        except Exception:  # noqa: BLE001
            return os.path.splitext(path)[1] or ".mp3"
        if "flac" in fmt:
            return ".flac"
        if "mp4" in fmt or "m4a" in fmt or "mov" in fmt:
            return ".m4a"
        if "ogg" in fmt:
            return ".ogg"
        if "wav" in fmt:
            return ".wav"
        return ".mp3"

    # ---------------- 曲库 ----------------
    def _library_files(self) -> list[str]:
        rows: list[str] = []
        for folder in self._library_dirs():
            if not os.path.isdir(folder):
                continue
            for path in sorted(glob.glob(os.path.join(folder, "*"))):
                if path.lower().endswith(AUDIO_EXT):
                    rows.append(path)
        return rows

    def _probe_cached(self, path: str) -> float:
        """ffprobe 有点贵，按 mtime+size 缓一下，别每次点歌都全库探一遍。"""
        try:
            st = os.stat(path)
            key = f"{path}|{int(st.st_mtime)}|{st.st_size}"
        except OSError:
            return 0.0
        if key not in self._duration_cache:
            self._duration_cache[key] = _probe(path)
        return self._duration_cache[key]

    @staticmethod
    def _py_keys(name: str) -> tuple[str, str]:
        """全拼 / 首字母。没装 pypinyin 就返回空串，不影响原有关键词匹配。"""
        try:
            from pypinyin import Style, lazy_pinyin
        except ImportError:
            return "", ""
        try:
            full = "".join(lazy_pinyin(name)).lower()
            initials = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER)).lower()
            return full, initials
        except Exception:  # noqa: BLE001
            return "", ""

    def _search_local(self, keyword: str, limit: int) -> list[dict]:
        hits = []
        kw = keyword.lower().strip()
        ascii_kw = kw.isascii()
        for path in self._library_files():
            base = os.path.basename(path)
            hay = base.lower()
            matched = kw in hay
            if not matched and ascii_kw:
                full, initials = self._py_keys(os.path.splitext(base)[0])
                matched = bool(full) and (kw in full or kw in initials)
            if not matched:
                continue
            stem = os.path.splitext(base)[0]
            # 曲库命名习惯是「歌名 - 歌手」，拆出来比一律显示"本地曲库"好看
            name, artist = stem, "本地曲库"
            if " - " in stem:
                left, right = stem.rsplit(" - ", 1)
                if left.strip() and right.strip():
                    name, artist = left.strip(), right.strip()
            hits.append({
                "id": path, "local": path, "source": "local",
                "name": name, "artist": artist,
                "seconds": int(self._probe_cached(path) or 0),
                "cover": "",
            })
        return hits[:limit]

    # ---------------- 网易云 ----------------
    def _netease_get(self, url: str, timeout: int = 15) -> bytes:
        headers = {"User-Agent": NETEASE_UA, "Referer": "https://music.163.com/"}
        cookie = str(self._cfg("netease_cookie", "") or "").strip()
        if cookie:
            headers["Cookie"] = cookie
        last: Exception | None = None
        for _ in range(2):           # 网易云偶尔抽风，给一次重试
            try:
                with urllib.request.urlopen(
                        urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
                    return resp.read()
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise last if last else RuntimeError("网易云请求失败")

    def _search_netease(self, keyword: str, limit: int) -> list[dict]:
        url = ("https://music.163.com/api/search/get/web?s="
               + urllib.parse.quote(keyword) + f"&type=1&limit={limit}")
        try:
            data = json.loads(self._netease_get(url).decode("utf-8", "ignore") or "{}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DeepseaRadio] 网易云搜索失败：{exc}")
            return []
        out = []
        for s in (data.get("result") or {}).get("songs") or []:
            album = s.get("album") or {}
            out.append({
                "id": s.get("id"), "local": "", "source": "netease",
                "name": s.get("name", ""),
                "artist": "、".join(a.get("name", "") for a in (s.get("artists") or []) if a),
                "seconds": round((s.get("duration") or 0) / 1000),
                "cover": album.get("picUrl") or "",
            })
        return out

    def _netease_url(self, song_id) -> str:
        url = ("https://music.163.com/api/song/enhance/player/url"
               f"?id={song_id}&ids=%5B{song_id}%5D&br=320000")
        try:
            data = json.loads(self._netease_get(url).decode("utf-8", "ignore") or "{}")
        except Exception:  # noqa: BLE001
            return ""
        items = data.get("data") or []
        return str(items[0].get("url") or "") if items else ""

    # ---------------- B 站 ----------------
    def _bili_cookie(self, refresh: bool = False) -> str:
        """SESSDATA（可选）+ 自动取的 buvid；没有 buvid 会 412。"""
        parts = []
        user = str(self._cfg("bilibili_cookie", "") or "").strip()
        if user:
            parts.append(user)
        if refresh or not self._bili_buvid:
            try:
                req = urllib.request.Request(
                    "https://api.bilibili.com/x/frontend/finger/spi",
                    headers={"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8", "ignore") or "{}")
                b3 = (data.get("data") or {}).get("b_3") or ""
                b4 = (data.get("data") or {}).get("b_4") or ""
                self._bili_buvid = f"buvid3={b3}; buvid4={b4}" if b3 else ""
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[DeepseaRadio] 取 buvid 失败：{exc}")
        if self._bili_buvid:
            parts.append(self._bili_buvid)
        return "; ".join(parts)

    def _bili_get(self, url: str, timeout: int = 20) -> dict:
        for attempt in (1, 2):
            headers = {"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}
            cookie = self._bili_cookie(refresh=attempt == 2)
            if cookie:
                headers["Cookie"] = cookie
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", "ignore") or "{}")
            except urllib.error.HTTPError as exc:
                if exc.code == 412 and attempt == 1:   # 风控，换个 buvid 再来
                    logger.info("[DeepseaRadio] B 站 412，换 buvid 重试")
                    continue
                raise
        return {}

    def _search_bili(self, keyword: str, limit: int) -> list[dict]:
        """按需翻页：B站一页 20 条，要 27 首就得多要一页。"""
        base = ("https://api.bilibili.com/x/web-interface/search/type"
                "?search_type=video&keyword=" + urllib.parse.quote(keyword))
        if bool(self._cfg("bili_music_only", True)):
            base += "&tids=3"          # 3 = 音乐区，不传就会塞一堆无关视频
        rows: list[dict] = []
        title_chars = int(self._cfg("title_chars", 26) or 26)
        pushed: set[str] = set()
        page = 1
        while len(rows) < limit and page <= 3:
            try:
                data = self._bili_get(f"{base}&page={page}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[DeepseaRadio] B 站搜索失败（第{page}页）：{exc}")
                break
            result = (data.get("data") or {}).get("result") or []
            if not result:
                break
            for v in result:
                bvid = v.get("bvid")
                if not bvid or bvid in pushed:
                    continue
                title = html.unescape(re.sub(r"<[^>]+>", "", v.get("title") or "")).strip()
                if title_chars and len(title) > title_chars:
                    title = title[: title_chars - 1] + "…"
                secs = _mmss_to_secs(v.get("duration") or "")
                if any(h in title for h in LOOP_HINTS) or not 0 < secs <= MAX_ONLINE_SECONDS:
                    continue
                pushed.add(bvid)
                pic = v.get("pic") or ""
                if pic.startswith("//"):
                    pic = "https:" + pic
                rows.append({
                    "id": bvid, "local": "", "source": "bilibili",
                    "name": title, "artist": v.get("author") or "", "seconds": secs,
                    "cover": pic,
                })
                if len(rows) >= limit:
                    break
            page += 1
        return rows[:limit]

    def _bili_audio(self, bvid: str) -> tuple[str, float]:
        """优先 FLAC（要大会员 cookie），否则挑码率最高的 AAC。"""
        info = self._bili_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
        data = info.get("data") or {}
        cid, secs = data.get("cid"), float(data.get("duration") or 0)
        if not cid:
            return "", secs
        play = self._bili_get(
            "https://api.bilibili.com/x/player/playurl"
            f"?bvid={bvid}&cid={cid}&fnval=16&fnver=0&fourk=1")
        pdata = play.get("data") or {}
        dash = pdata.get("dash") or {}
        flac = (dash.get("flac") or {}).get("audio") if dash.get("flac") else None
        if flac and flac.get("baseUrl"):
            return flac["baseUrl"], secs
        audios = dash.get("audio") or []
        if audios:
            best = sorted(audios, key=lambda a: -(a.get("bandwidth") or 0))[0]
            if best.get("baseUrl"):
                return best["baseUrl"], secs
        durl = pdata.get("durl") or []
        if durl and durl[0].get("url"):
            return durl[0]["url"], secs
        return "", secs

    # ---------------- 搜索聚合 / 下载 ----------------
    def _search_all(self, keyword: str, limit: int) -> list[dict]:
        """三源并发查，再按「同名同歌手」去重（本地优先）。"""
        wanted = [str(x).strip().lower() for x in (self._cfg("sources", ["local", "bilibili"]) or [])]
        jobs = []
        if "local" in wanted:
            jobs.append(self._search_local)
        if "bilibili" in wanted:
            jobs.append(self._search_bili)
        if "netease" in wanted:
            jobs.append(self._search_netease)
        if not jobs:
            return []

        from concurrent.futures import ThreadPoolExecutor
        results: list[list[dict]] = []
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(fn, keyword, limit) for fn in jobs]
            for fut in futures:
                try:
                    results.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[DeepseaRadio] 某个源挂了：{exc}")

        def norm(text: object) -> str:
            return re.sub(r"\s+", "", str(text or "")).lower()

        local_names = {
            norm(song.get("name"))[:20]
            for chunk in results for song in chunk
            if song.get("source") == "local"
        }
        seen: set[tuple[str, str]] = set()
        merged: list[dict] = []
        for chunk in results:
            for song in chunk:
                key = (norm(song.get("name")), norm(song.get("artist")))
                if key in seen:
                    continue
                # 本地已经有的歌，在线源再出现就别重复塞了（B站标题常被截断，
                # 所以只比前 20 个字，够用且不容易误伤不同版本）
                if song.get("source") != "local" and norm(song.get("name"))[:20] in local_names:
                    continue
                seen.add(key)
                merged.append(song)
        return merged[:limit]

    def _download(self, url: str, suffix: str = ".mp3", referer: str = "") -> str:
        out = os.path.join(tempfile.gettempdir(), f"dsr_dl_{os.getpid()}_{os.urandom(3).hex()}{suffix}")
        headers = {"User-Agent": NETEASE_UA,
                   "Referer": referer or ("https://www.bilibili.com/" if "bilivideo" in url or "mcdn" in url
                                          else "https://music.163.com/")}
        cookie = str(self._cfg("bilibili_cookie", "") or "").strip()
        if cookie and ("bilivideo" in url or "mcdn" in url):
            headers["Cookie"] = cookie
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as resp, \
                open(out, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        if os.path.getsize(out) < 10240:
            os.remove(out)
            raise RuntimeError("下载到的文件太小，八成不是音频")
        return out

    def _fetch(self, song: dict) -> str:
        """把在线歌曲落到本地临时文件。"""
        if song.get("local"):
            return str(song["local"])
        if song.get("source") == "netease":
            url = self._netease_url(song["id"])
            if not url:
                raise RuntimeError("网易云没给链接（要配 netease_cookie，且账号权限够）")
            return self._download(url)
        url, secs = self._bili_audio(str(song["id"]))
        if not url:
            raise RuntimeError("B 站没给出可用音轨")
        song["seconds"] = int(secs or song.get("seconds") or 0)
        return self._download(url, referer="https://www.bilibili.com/")

    def _sweep_temp(self, max_age: int = 3600) -> None:
        """清掉自己留下的临时音频。家政插件已经停用，不扫就没人扫了。"""
        tmp = tempfile.gettempdir()
        now = time.time()
        try:
            names = os.listdir(tmp)
        except OSError:
            return
        for name in names:
            if not name.startswith(("dsr_", "a2v_", "a2f_")):
                continue
            path = os.path.join(tmp, name)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.remove(path)
            except OSError:
                pass

    # ---------------- 直链点歌 ----------------
    def _song_by_bvid(self, bvid: str) -> dict | None:
        try:
            data = self._bili_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DeepseaRadio] 查 BV 失败：{exc}")
            return None
        d = data.get("data") or {}
        if not d.get("bvid"):
            return None
        pic = str(d.get("pic") or "")
        if pic.startswith("//"):
            pic = "https:" + pic
        title = str(d.get("title") or bvid)
        if len(title) > 40:
            title = title[:39] + "…"
        return {
            "id": bvid, "local": "", "source": "bilibili", "name": title,
            "artist": (d.get("owner") or {}).get("name") or "",
            "seconds": int(d.get("duration") or 0), "cover": pic,
        }

    def _song_by_netease_id(self, sid: str) -> dict | None:
        try:
            raw = self._netease_get(f"https://music.163.com/api/song/detail?ids=[{sid}]")
            data = json.loads(raw.decode("utf-8", "ignore") or "{}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DeepseaRadio] 查网易云 id 失败：{exc}")
            return None
        songs = data.get("songs") or []
        if not songs:
            return None
        s = songs[0]
        album = s.get("album") or {}
        return {
            "id": s.get("id") or sid, "local": "", "source": "netease",
            "name": s.get("name", ""),
            "artist": "、".join(a.get("name", "") for a in (s.get("artists") or []) if a),
            "seconds": round((s.get("duration") or 0) / 1000),
            "cover": album.get("picUrl", ""),
        }

    def _direct_song(self, text: str) -> dict | None:
        """点歌后面直接给 BV 号 / 网易云链接时，别去搜了，直接认。"""
        m = re.search(r"BV[0-9A-Za-z]{10}", text)
        if m:
            return self._song_by_bvid(m.group(0))
        if "music.163.com" in text:
            m = re.search(r"song(?:\?|/)(?:[^\s]*?[?&])?id=(\d+)", text) or re.search(r"id=(\d+)", text)
            if m:
                return self._song_by_netease_id(m.group(1))
        m = re.fullmatch(r"(?:网易云)?\s*(\d{6,12})", text.strip())
        if m:
            return self._song_by_netease_id(m.group(1))
        return None

    # ---------------- 点歌榜 ----------------
    def _load_plays(self) -> dict:
        try:
            with open(self._plays_file, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault("songs", {})
                data.setdefault("users", {})
                return data
        except (OSError, ValueError):
            pass
        return {"songs": {}, "users": {}}

    def _bump_play(self, song: dict, user_id: str) -> None:
        key = f"{song.get('name', '')} - {song.get('artist', '')}".strip(" -")
        if not key:
            return
        songs = self._plays.setdefault("songs", {})
        songs[key] = int(songs.get(key, 0)) + 1
        users = self._plays.setdefault("users", {})
        users[str(user_id)] = int(users.get(str(user_id), 0)) + 1
        try:
            with open(self._plays_file, "w", encoding="utf-8") as fh:
                json.dump(self._plays, fh, ensure_ascii=False)
        except OSError as exc:
            logger.warning(f"[DeepseaRadio] 榜单落盘失败：{exc}")

    async def cmd_rank(self, event: AstrMessageEvent):
        """点歌榜：这台电台被点得最多的歌。"""
        songs = self._plays.get("songs") or {}
        if not songs:
            yield event.plain_result("榜单还是空的，点一首就能上榜。")
            event.stop_event()
            return
        top = sorted(songs.items(), key=lambda kv: -int(kv[1]))[:10]
        lines = ["🐋 深海电台 · 点歌榜"]
        for i, (name, count) in enumerate(top, 1):
            lines.append(f"{i}. {name}　×{count}")
        yield event.plain_result(chr(10).join(lines))
        event.stop_event()

    async def cmd_my_plays(self, event: AstrMessageEvent):
        """我的点歌：自己点过几首。"""
        try:
            uid = str(event.get_sender_id())
        except Exception:  # noqa: BLE001
            uid = ""
        count = int((self._plays.get("users") or {}).get(uid, 0))
        yield event.plain_result(f"你在这台电台上点过 {count} 首。" if count else "你还没点过歌呢。")
        event.stop_event()

    def _maybe_auto_clean(self) -> None:
        """按间隔自动清缓存：封面按数量留，临时音频按小时删，过期会话丢掉。"""
        hours = float(self._cfg("auto_clean_hours", 6) or 0)
        if hours <= 0:
            return
        if time.time() - self._last_clean < hours * 3600:
            return
        self._last_clean = time.time()
        self._sweep_temp(max_age=3600)
        self._clean_covers(int(self._cfg("cache_max_files", 200) or 200))
        ttl = float(self._cfg("session_ttl", 600) or 600)
        now = time.time()
        dead = [k for k, v in self._sessions.items()
                if now - float(v.get("created", 0)) > ttl]
        if dead:
            for k in dead:
                self._sessions.pop(k, None)
            self._save_sessions()
        logger.info(f"[DeepseaRadio] 自动清理完成（间隔 {hours}h）")

    # ---------------- 封面拼图 ----------------
    # 一页最多 9 首，拼成 3 列一张图发出去；单张封面图会刷屏，拼图刚好。
    def _cover_dir(self) -> str:
        path = os.path.join(self.plugin_dir, "cache")
        os.makedirs(path, exist_ok=True)
        return path

    def _cover_cache_path(self, key: str) -> str:
        safe = re.sub(r"[^0-9a-zA-Z_-]", "_", key)[:80]
        return os.path.join(self._cover_dir(), safe + ".jpg")

    @staticmethod
    def _cover_url(url: str) -> str:
        """B站/网易云的图都支持 ?param=WxH：统一要 500 方图，清晰又不至于被平台压糊。"""
        if not url:
            return url
        base = url.split("?")[0]
        # 网易云原图动辄几 MB：要 500 方图。B站不支持 ?param=（会原样返回），
        # 它的原图 1700x1080 / 100KB 左右，已经够清楚，就别再压了。
        if "126.net" in base:
            return base + "?param=500y500"
        return base

    def _fetch_cover(self, url: str, key: str) -> str | None:
        if not url:
            return None
        path = self._cover_cache_path(key)
        if os.path.exists(path) and os.path.getsize(path) > 1024:
            return path
        url = self._cover_url(url)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read()
            if len(data) < 1024:
                return None
            with open(path, "wb") as fh:
                fh.write(data)
            return path
        except Exception:  # noqa: BLE001
            return None

    def _local_cover(self, path: str) -> str | None:
        """本地封面：先找同名图片，再抽内嵌封面；都没有就返回 None。"""
        out = self._cover_cache_path("local_" + os.path.basename(path))
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            return out
        stem = os.path.splitext(path)[0]
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
            side = stem + ext
            if os.path.exists(side) and os.path.getsize(side) > 1024:
                return side
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
                 "-an", "-vcodec", "copy", "-frames:v", "1", out],
                check=True, timeout=20, capture_output=True)
        except Exception:  # noqa: BLE001
            return None
        return out if os.path.exists(out) and os.path.getsize(out) > 1024 else None

    def _grid(self, songs: list[dict], start_index: int) -> str | None:
        """把一页歌的封面拼成一张图，左上角标编号。"""
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageOps
        except ImportError:
            return None
        cell, gap, cols = 240, 10, 3
        rows = max(1, (len(songs) + cols - 1) // cols)
        width = cols * cell + (cols + 1) * gap
        height = rows * cell + (rows + 1) * gap
        canvas = Image.new("RGB", (width, height), (22, 30, 42))
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
        except Exception:  # noqa: BLE001
            font = None

        for i, song in enumerate(songs):
            row, col = divmod(i, cols)
            x = gap + col * (cell + gap)
            y = gap + row * (cell + gap)
            cover = None
            if song.get("local"):
                cover = self._local_cover(str(song["local"]))
            else:
                cover = self._fetch_cover(
                    str(song.get("cover") or ""),
                    f'{song.get("source", "x")}_{song.get("id", i)}')
            if cover:
                try:
                    img = Image.open(cover).convert("RGB")
                    canvas.paste(ImageOps.fit(img, (cell, cell)), (x, y))
                except Exception:  # noqa: BLE001
                    draw.rectangle([x, y, x + cell, y + cell], fill=(40, 52, 70))
            else:
                # 本地曲库常常没有封面（也没有中文字体可画标题），给个音符占位
                draw.rectangle([x, y, x + cell, y + cell], fill=(40, 52, 70))
                try:
                    note = ImageFont.truetype(
                        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 96)
                    draw.text((x + cell // 2 - 34, y + cell // 2 - 58), "\u266a",
                              fill=(120, 140, 165), font=note)
                except Exception:  # noqa: BLE001
                    pass
            label = str(start_index + i)
            box_w = 34 + 22 * len(label)
            draw.rectangle([x, y, x + box_w, y + 44], fill=(0, 0, 0))
            draw.text((x + 12, y + 4), label, fill=(255, 255, 255), font=font)

        out = os.path.join(self._cover_dir(), f"grid_{os.getpid()}_{os.urandom(3).hex()}.jpg")
        canvas.save(out, quality=88)
        self._clean_covers()
        return out

    def _clean_covers(self, keep: int | None = None) -> None:
        if keep is None:
            keep = int(self._cfg("cache_max_files", 200) or 200)
        """封面缓存按时间留最近 N 个。以前只清 grid_，单曲封面会无限堆。"""
        try:
            files = [os.path.join(self._cover_dir(), f) for f in os.listdir(self._cover_dir())]
            files = [f for f in files if os.path.isfile(f)]
            if len(files) <= keep:
                return
            files.sort(key=os.path.getmtime)
            for path in files[: len(files) - keep]:
                os.remove(path)
        except OSError:
            pass

    def _placeholder(self, idx: int, name: str = "") -> str | None:
        """没有封面时给一张占位卡：歌名首字 + 音符，保证每条歌都有图。"""
        try:
            from PIL import Image as PILImage, ImageDraw
        except ImportError:
            return None
        out = os.path.join(self._cover_dir(), f"ph_{idx}_{abs(hash(name)) % 999}.jpg")
        if os.path.exists(out) and os.path.getsize(out) > 512:
            return out
        cell = 300
        img = PILImage.new("RGB", (cell, cell), (34, 44, 60))
        draw = ImageDraw.Draw(img)
        # 跳过开头的标点/括号，取第一个像字的字符
        match = re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", name or "")
        head = match.group(0) if match else "\u266a"
        font = self._font(96)
        try:
            width = draw.textlength(head, font=font)
            draw.text(((cell - width) / 2, cell / 2 - 70), head, fill=(150, 175, 210), font=font)
        except Exception:  # noqa: BLE001
            pass
        small = self._font(30)
        draw.text((cell / 2 - 12, cell - 62), "\u266a", fill=(110, 130, 160), font=small)
        try:
            img.save(out, quality=85)
            self._clean_covers()
            return out
        except OSError:
            return None

    def _font(self, size: int):
        """中文字体（插件自带文泉驿，系统里没有中文字体也没关系）。"""
        from PIL import ImageFont
        cache = getattr(self, "_font_cache", None)
        if cache is None:
            cache = self._font_cache = {}
        if size not in cache:
            path = os.path.join(self.plugin_dir, "assets", "fonts", "wqy-zenhei.ttc")
            try:
                cache[size] = ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                cache[size] = ImageFont.load_default()
        return cache[size]

    @staticmethod
    def _ellipsis(draw, text: str, font, max_width: float) -> str:
        if not text:
            return ""
        try:
            if draw.textlength(text, font=font) <= max_width:
                return text
        except Exception:  # noqa: BLE001
            return text[:12]
        out = text
        while out and draw.textlength(out + "…", font=font) > max_width:
            out = out[:-1]
        return (out + "…") if out else ""

    def _poster(self, songs: list[dict], start_index: int, sess: dict) -> str | None:
        """整页渲染成一张图文海报：封面 + 编号 + 歌名 + 歌手 + 时长。"""
        try:
            from PIL import Image as PILImage, ImageDraw, ImageOps
        except ImportError:
            return None
        cols, pad = 3, 16
        cell_w, cover_h, text_h = 300, 300, 84
        row_h = cover_h + text_h
        rows = max(1, (len(songs) + cols - 1) // cols)
        page_size = int(self._cfg("page_size", 9) or 9)
        total_pages = max(1, int(-(-len(sess["songs"]) // page_size)))

        width = cols * cell_w + (cols + 1) * pad
        head_h = 58
        height = head_h + rows * row_h + (rows + 1) * pad
        canvas = PILImage.new("RGB", (width, height), (16, 20, 28))
        draw = ImageDraw.Draw(canvas)

        title = f"《{sess['keyword']}》 {len(sess['songs'])} 首 · 第 {sess['page'] + 1}/{total_pages} 页"
        draw.text((pad, 14), self._ellipsis(draw, title, self._font(26), width - pad * 2),
                  fill=(230, 236, 246), font=self._font(26))

        name_font = self._font(22)
        meta_font = self._font(17)
        for i, song in enumerate(songs):
            row, col = divmod(i, cols)
            x = pad + col * (cell_w + pad)
            y = head_h + pad + row * (row_h + pad)
            cover = self._cover_for(song) or self._placeholder(start_index + i, str(song.get('name') or ''))
            if cover:
                try:
                    img = PILImage.open(cover).convert("RGB")
                    canvas.paste(ImageOps.fit(img, (cell_w, cover_h)), (x, y))
                except Exception:  # noqa: BLE001
                    draw.rectangle([x, y, x + cell_w, y + cover_h], fill=(40, 52, 70))
            else:
                draw.rectangle([x, y, x + cell_w, y + cover_h], fill=(40, 52, 70))

            # 编号角标
            label = str(start_index + i)
            draw.rectangle([x, y, x + 46, y + 40], fill=(0, 0, 0))
            draw.text((x + 10, y + 5), label, fill=(255, 255, 255), font=self._font(26))

            ty = y + cover_h + 6
            draw.text((x + 2, ty), self._ellipsis(draw, str(song.get("name") or ""), name_font, cell_w - 4),
                      fill=(232, 238, 248), font=name_font)
            artist = self._ellipsis(draw, str(song.get("artist") or ""), meta_font, cell_w - 70)
            draw.text((x + 2, ty + 32), artist, fill=(146, 160, 182), font=meta_font)
            dur = _mmss(song.get("seconds") or 0)
            draw.text((x + cell_w - 52, ty + 32), dur, fill=(146, 160, 182), font=meta_font)

        out = os.path.join(self._cover_dir(), f"poster_{os.getpid()}_{os.urandom(3).hex()}.jpg")
        try:
            canvas.save(out, quality=88)
        except OSError:
            return None
        self._clean_covers()
        return out

    def _cover_for(self, song: dict) -> str | None:
        """取这首歌的封面文件：本地抽内嵌/同名图，在线抓 URL。"""
        if song.get("local"):
            return self._local_cover(str(song["local"]))
        return self._fetch_cover(
            str(song.get("cover") or ""),
            f'{song.get("source", "x")}_{song.get("id", "")}')

    def _entry_line(self, idx: int, song: dict) -> str:
        tag = {"local": "曲库", "bilibili": "B站", "netease": "网易云"}.get(song.get("source"), "")
        quality = song.get("quality") or ""
        extra = "·".join([x for x in (tag, quality) if x])
        return (f"{idx}. {song['name']} - {song['artist']}（{_mmss(song.get('seconds') or 0)}）"
                + (f"　[{extra}]" if extra else ""))

    async def _send_page(self, event: AstrMessageEvent, sess: dict):
        """发一页。cover_style=grid 拼九宫格；inline 每条歌后跟自己的封面。"""
        page_size = int(self._cfg("page_size", 9) or 9)
        start = sess["page"] * page_size
        chunk = sess["songs"][start:start + page_size]
        if bool(self._cfg("show_quality", False)) and chunk:
            try:
                await asyncio.to_thread(self._probe_page_quality, chunk)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[DeepseaRadio] 探音质失败：{exc}")

        style = str(self._cfg("cover_style", "grid") or "grid").lower()
        if not bool(self._cfg("show_covers", True)):
            style = "off"

        if style == "poster" and chunk:
            poster = await asyncio.to_thread(self._poster, chunk, start + 1, sess)
            if poster:
                try:
                    yield event.image_result(poster)
                    yield event.plain_result(self._page_tips(sess))
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[DeepseaRadio] 海报发送失败，退回拼图：{exc}")
            style = "grid"          # 海报挂了就退回九宫格

        if style == "inline" and chunk:
            total_pages = max(1, int(-(-len(sess["songs"]) // page_size)))
            # 一条消息里文字和图交错排好：1.歌名 图 / 2.歌名 图 …（合在一起，不刷屏）
            chain: list = [Plain(text=(
                f"《{sess['keyword']}》搜到 {len(sess['songs'])} 首，"
                f"第 {sess['page'] + 1}/{total_pages} 页："))]
            for i, song in enumerate(chunk, start + 1):
                chain.append(Plain(text=self._entry_line(i, song)))
                cover = await asyncio.to_thread(self._cover_for, song)
                if not cover:
                    cover = await asyncio.to_thread(self._placeholder, i, str(song.get('name') or ''))
                if cover:
                    try:
                        chain.append(Image.fromFileSystem(cover))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[DeepseaRadio] 封面加载失败：{exc}")
            chain.append(Plain(text=self._page_tips(sess)))
            try:
                yield event.chain_result(chain)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[DeepseaRadio] 图文合并发送失败，退回纯文字：{exc}")
                yield event.plain_result(self._render_page(sess))
            return

        if style == "grid" and chunk:
            try:
                grid = await asyncio.to_thread(self._grid, chunk, start + 1)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[DeepseaRadio] 拼封面失败：{exc}")
                grid = None
            if grid:
                try:
                    yield event.image_result(grid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[DeepseaRadio] 封面发送失败：{exc}")
        yield event.plain_result(self._render_page(sess))

    def _page_tips(self, sess: dict) -> str:
        page_size = int(self._cfg("page_size", 9) or 9)
        total_pages = max(1, int(-(-len(sess["songs"]) // page_size)))
        tips = ["回编号选歌", "「编号 文件」发原轨"]
        if sess["page"] + 1 < total_pages:
            tips.append("「下」翻页")
        if sess["page"] > 0:
            tips.append("「上」回上一页")
        tips.append("「取消」退出")
        return "｜".join(tips)


    # ---------------- 会话 ----------------
    def _sess_key(self, event: AstrMessageEvent) -> str:
        try:
            sender = event.get_sender_id()
        except Exception:  # noqa: BLE001
            sender = "anon"
        try:
            origin = event.unified_msg_origin
        except Exception:  # noqa: BLE001
            origin = "here"
        return f"{sender}@{origin}"

    def _session_alive(self, sess: dict) -> bool:
        return bool(sess) and (time.time() - sess.get("created", 0) <= float(self._cfg("session_ttl", 600) or 600))

    def _cooldown_left(self, event: AstrMessageEvent) -> int:
        until = self._cooldowns.get(self._sess_key(event), 0.0)
        return max(0, int(until - time.time()))

    def _bili_quality(self, bvid: str) -> str:
        """探一首 B站视频的音质标签（有无 FLAC / 最高码率）。"""
        try:
            info = self._bili_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
            cid = (info.get("data") or {}).get("cid")
            if not cid:
                return ""
            play = self._bili_get(
                "https://api.bilibili.com/x/player/playurl"
                f"?bvid={bvid}&cid={cid}&fnval=16&fnver=0&fourk=1")
            dash = (play.get("data") or {}).get("dash") or {}
            if (dash.get("flac") or {}).get("audio"):
                return "无损"
            best = max((int(a.get("bandwidth") or 0) for a in dash.get("audio") or []), default=0)
        except Exception:  # noqa: BLE001
            return ""
        if best >= 900_000:
            return "无损"
        if best >= 256_000:
            return "256k+"
        if best >= 192_000:
            return "192k"
        if best >= 128_000:
            return "128k"
        return f"{max(1, best // 1000)}k" if best else ""

    def _probe_page_quality(self, songs: list[dict]) -> None:
        """给这一页的在线曲目补音质标签（并发 4 个，别把 B站惹毛）。"""
        todo = [s for s in songs if not s.get("quality") and s.get("source") == "bilibili"]
        if not todo:
            return
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            labels = list(pool.map(lambda x: self._bili_quality(str(x["id"])), todo))
        for song, label in zip(todo, labels):
            if label:
                song["quality"] = label

    def _render_page(self, sess: dict) -> str:
        songs = sess["songs"]
        page = sess["page"]
        page_size = int(self._cfg("page_size", 9) or 9)
        total_pages = max(1, int(-(-len(songs) // page_size)))
        start = page * page_size
        lines = []
        for i, s in enumerate(songs[start:start + page_size], start + 1):
            tag = {"local": "曲库", "bilibili": "B站", "netease": "网易云"}.get(s.get("source"), "")
            quality = s.get("quality") or ""
            extra = "·".join([x for x in (tag, quality) if x])
            lines.append(f"{i}. {s['name']} - {s['artist']}（{_mmss(s.get('seconds') or 0)}）"
                         + (f"　[{extra}]" if extra else ""))
        head = f"《{sess['keyword']}》搜到 {len(songs)} 首，第 {page + 1}/{total_pages} 页："
        tips = ["回编号选歌", "「编号 文件」发原轨"]
        if page + 1 < total_pages:
            tips.append("「下」翻页")
        if page > 0:
            tips.append("「上」回上一页")
        tips.append("「取消」退出")
        return head + "\n" + "\n".join(lines) + "\n" + "｜".join(tips)

    # ---------------- 发送 ----------------
    async def _send_song(self, event: AstrMessageEvent, sess: dict, fmt: str = "voice"):
        song = sess["chosen"]
        try:
            src = await asyncio.to_thread(self._fetch, song)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(
                f"《{song['name']}》没拿到音源：{exc}\n这次不算冷却，可以再选一个编号，或「文件」换格式。")
            return
        total = await asyncio.to_thread(_probe, src)
        official = self._is_official(event)
        try:
            if fmt == "file":
                ext = self._real_ext(src)
                name = self._safe_name(f"{song['name']} - {song['artist']}") + ext
                chain = [File(name=name, file=src)]
                if bool(self._cfg("reply_hint", True)):
                    chain.insert(0, Plain(text=f"《{song['name']}》{_mmss(total)}，原轨给你："))
            else:
                how, limit = self._plan(event, total)
                if how == "file":
                    ext = self._real_ext(src)
                    chain = [File(name=self._safe_name(song["name"]) + ext, file=src)]
                    chain.insert(0, Plain(text=f"《{song['name']}》{_mmss(total)} 太长，切成文件发你。"))
                else:
                    pieces = await asyncio.to_thread(
                        self._pieces, src, total, limit if how == "segments" else 0, official)
                    chain = [Record.fromFileSystem(p) for p in pieces]
                    if bool(self._cfg("reply_hint", True)):
                        note = f"《{song['name']}》{_mmss(total)}"
                        if how == "segments":
                            note += f"，单条限 {_mmss(limit)}，切 {len(pieces)} 段连发"
                        chain.insert(0, Plain(text=note + "，语音条来了。"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[DeepseaRadio] 发送失败：{exc}")
            yield event.plain_result(f"发送失败：{type(exc).__name__}: {exc}")
            return

        # B站来源可选带一张分享卡片（客户端不认就只是多一段，不影响音频）
        if (bool(self._cfg("bili_card", False)) and song.get("source") == "bilibili"
                and isinstance(chain, list)):
            try:
                chain.insert(0, RawJson(payload=json.dumps(self._bili_card_payload(song), ensure_ascii=False)))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[DeepseaRadio] 卡片附加失败：{exc}")

        if bool(self._cfg("save_downloaded", True)) and not song.get("local"):
            try:
                safe = self._safe_name(f"{song['name']} - {song['artist']}")
                dst = os.path.join(self._library_root(), safe + self._real_ext(src))
                if not os.path.exists(dst):
                    shutil.copyfile(src, dst)
            except OSError as exc:
                logger.warning(f"[DeepseaRadio] 顺手入库失败：{exc}")

        self._sessions.pop(self._sess_key(event), None)
        self._save_sessions()
        try:
            self._bump_play(song, event.get_sender_id())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DeepseaRadio] 榜单记录失败：{exc}")
        self._cooldowns[self._sess_key(event)] = time.time() + float(self._cfg("cooldown", 60) or 0)
        yield event.chain_result(chain)

    # ---------------- 指令路由 ----------------
    # 不用 @filter.command：标准指令要求「@ 或唤醒前缀」才触发，
    # 群里直接打「点歌 倔强」时 is_at_or_wake_command 是 False，匹配不上、会被大模型接走。
    # 这里自己看文本开头，命中就处理并 stop_event，不跟大模型抢答。
    _ROUTES = (
        ("电台自检", "cmd_selftest"),
        ("B站卡片", "cmd_bili_card"),
        ("扫码登录", "cmd_bili_login"),
        ("B站登录", "cmd_bili_login"),
        ("点歌榜", "cmd_rank"),
        ("我的点歌", "cmd_my_plays"),
        ("登录状态", "cmd_login_status"),
        ("B站状态", "cmd_login_status"),
        ("转语音", "cmd_to_voice"),
        ("转音频", "cmd_to_audio"),
        ("点歌", "cmd_play"),
        ("来一首", "cmd_play"),
        ("歌单", "cmd_library"),
        ("曲库", "cmd_library"),
        ("收歌", "cmd_collect"),
        ("入库", "cmd_collect"),
    )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self.enabled:
            return
        text = " ".join((event.message_str or "").split())
        if not text:
            return
        for word, method in self._ROUTES:
            if text == word or text.startswith(word):
                logger.info(f"[DeepseaRadio] 命中「{word}」：{text[:24]}")
                try:
                    async for item in getattr(self, method)(event):
                        yield item
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[DeepseaRadio] {word} 执行失败：{exc}", exc_info=True)
                    yield event.plain_result(f"{word} 出错了：{type(exc).__name__}: {exc}")
                event.stop_event()
                return

    async def cmd_selftest(self, event: AstrMessageEvent):
        """电台自检：把链路上每一环都摸一遍。"""
        lines = ["🐋 深海电台自检"]
        lines.append(f"· 插件已加载，启用={self.enabled}")

        files = await asyncio.to_thread(self._library_files)
        lines.append(f"· 本地曲库：{len(files)} 首（{self._library_dirs() or '未配置'}）")

        sources = self._cfg("sources", ["local", "bilibili"])
        lines.append(f"· 搜索源：{', '.join(sources)}")

        bili_ok = "未启用"
        if "bilibili" in sources:
            try:
                rows = await asyncio.to_thread(self._search_bili, "稻香", 1)
                bili_ok = f"通（示例：{rows[0]['name'][:14]}）" if rows else "返回空"
            except Exception as exc:  # noqa: BLE001
                bili_ok = f"失败：{type(exc).__name__}"
        lines.append(f"· B站音乐区：{bili_ok}")

        netease_ok = "未启用"
        if "netease" in sources:
            if str(self._cfg("netease_cookie", "") or "").strip():
                try:
                    rows = await asyncio.to_thread(self._search_netease, "稻香", 1)
                    netease_ok = f"通（{len(rows)} 条）" if rows else "返回空"
                except Exception as exc:  # noqa: BLE001
                    netease_ok = f"失败：{type(exc).__name__}"
            else:
                netease_ok = "缺 netease_cookie，只能搜不能出声"
        lines.append(f"· 网易云：{netease_ok}")

        ffmpeg = shutil.which("ffmpeg")
        lines.append(f"· ffmpeg：{'在' if ffmpeg else '缺！'}｜pilk：{'在' if pilk else '缺（codec=wav 时不需要）'}")

        yield event.plain_result(chr(10).join(lines))
        event.stop_event()

    def _bili_card_payload(self, song: dict) -> dict:
        """QQ 结构化分享卡片（NapCat 走 json 段）。格式对客户端版本敏感，不保证每家都认。"""
        title = str(song.get("name") or "B站视频")
        desc = f"{song.get('artist') or '哔哩哔哩'} · {_mmss(song.get('seconds') or 0)}"
        jump = f"https://www.bilibili.com/video/{song.get('id')}"
        return {
            "app": "com.tencent.structmsg",
            "desc": "新闻",
            "view": "news",
            "ver": "0.0.0.1",
            "prompt": f"[分享] {title}",
            "meta": {
                "news": {
                    "title": title,
                    "desc": desc,
                    "jumpUrl": jump,
                    "preview": song.get("cover") or "",
                    "tag": "哔哩哔哩",
                    "appid": 100951776,
                }
            },
        }

    async def cmd_bili_card(self, event: AstrMessageEvent):
        """B站卡片 <BV号>：把视频做成分享卡片发出来（不行会自动退回链接）。"""
        text = event.message_str.strip()
        for token in ("B站卡片", "b站卡片"):
            if text.startswith(token):
                text = text[len(token):].strip()
                break
        m = re.search(r"BV[0-9A-Za-z]{10}", text)
        if not m:
            yield event.plain_result("给个 BV 号，例：B站卡片 BV1a8411o71x")
            event.stop_event()
            return
        song = await asyncio.to_thread(self._song_by_bvid, m.group(0))
        if not song:
            yield event.plain_result("没查到这条视频，检查下 BV 号。")
            event.stop_event()
            return
        payload = self._bili_card_payload(song)
        try:
            yield event.chain_result([RawJson(payload=json.dumps(payload, ensure_ascii=False))])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DeepseaRadio] 卡片发送失败：{exc}")
            yield event.plain_result(
                f"卡片发不出去（{type(exc).__name__}），给你链接："
                f"https://www.bilibili.com/video/{song['id']}")
        event.stop_event()

    async def cmd_login_status(self, event: AstrMessageEvent):
        """登录状态：B站 cookie 有效吗、登录了没、是不是大会员。"""
        cookie = str(self._cfg("bilibili_cookie", "") or "").strip()
        info = await asyncio.to_thread(bili_login.login_report, cookie)
        lines = ["🐋 B站登录状态"]
        if info["logged_in"]:
            lines.append(f"· 已登录：{info['uname'] or '（没拿到昵称）'}")
            lines.append(f"· 会员：{info['vip']}｜cookie {info['length']} 字节")
            lines.append("· 有大会员权限时，点 Hi-Res 视频会优先拿 FLAC 无损。")
        elif info["has_sessdata"]:
            lines.append("· cookie 里有 SESSDATA，但 B站说没登录——多半过期了。")
            lines.append("· 发「扫码登录」重扫一次。")
        elif info["has_cookie"]:
            lines.append("· 有 cookie 但没 SESSDATA，复制时可能漏了。发「扫码登录」重扫。")
        else:
            lines.append("· 还没登录。不登录也能用：搜歌、发 AAC 音轨、本地曲库点歌。")
            lines.append("· 想要 FLAC 无损，发「扫码登录」。")
        netease = str(self._cfg("netease_cookie", "") or "").strip()
        lines.append(f"· 网易云 cookie：{'有' if netease else '无（只能搜，不能出声）'}")
        yield event.plain_result(chr(10).join(lines))
        event.stop_event()

    async def cmd_bili_login(self, event: AstrMessageEvent):
        """扫码登录：发二维码，扫完自动存 cookie。"""
        try:
            key, content = await asyncio.to_thread(bili_login.qr_new)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"取二维码失败：{type(exc).__name__}: {exc}")
            event.stop_event()
            return
        if not key or not content:
            yield event.plain_result("B站没给二维码，稍后再试。")
            event.stop_event()
            return
        png = os.path.join(tempfile.gettempdir(), f"bili_qr_{os.getpid()}.png")
        try:
            await asyncio.to_thread(bili_login.qr_png, content, png)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"画二维码失败：{exc}")
            event.stop_event()
            return
        yield event.chain_result([
            Plain(text="用 B站 App 扫这个码（别用微信/相机），扫完点确认："),
            Image.fromFileSystem(png),
        ])
        cookie = str(self._cfg("bilibili_cookie", "") or "").strip()
        for _ in range(45):          # 最多等 90 秒
            await asyncio.sleep(2)
            try:
                code, _msg, new_cookie = await asyncio.to_thread(
                    bili_login.qr_poll_once, key, cookie)
            except Exception:  # noqa: BLE001
                continue
            if code == 0 and new_cookie:
                self.config["bilibili_cookie"] = new_cookie
                saved = bili_login.save_cookie(new_cookie)
                yield event.plain_result(
                    "登录成功，cookie 存好了。" + ("" if saved else "（写文件失败，重启前仍有效）"))
                event.stop_event()
                return
            if code == 86038:
                yield event.plain_result("二维码过期了，再发一次「扫码登录」。")
                event.stop_event()
                return
        yield event.plain_result("等了 90 秒没扫上，再发一次「扫码登录」吧。")
        event.stop_event()

    # ---------------- 指令 ----------------
    async def cmd_play(self, event: AstrMessageEvent):
        """点歌 <关键词>：搜索并列出歌单，回编号就发。"""
        if not self.enabled:
            return
        keyword = event.message_str.strip()
        for token in ("点歌", "来一首"):
            if keyword.startswith(token):
                keyword = keyword[len(token):].strip()
                break
        if not keyword:
            yield event.plain_result("格式：点歌 <关键词>\n例：点歌 倔强 五月天")
            return
        left = self._cooldown_left(event)
        if left > 0:
            yield event.plain_result(f"刚点过，冷却中，还剩 {left} 秒。")
            return
        # 直接给 BV 号 / 网易云链接 / 数字 id：不搜了，认了直接发
        direct = await asyncio.to_thread(self._direct_song, keyword)
        if direct:
            sess = {"step": "pick", "page": 0, "songs": [direct], "chosen": direct,
                    "keyword": keyword, "created": time.time()}
            yield event.plain_result(
                f"认得这首：《{direct['name']}》- {direct['artist'] or '未知'}（{_mmss(direct['seconds'])}）")
            async for item in self._send_song(event, sess, "voice"):
                yield item
            event.stop_event()
            return

        limit = max(1, min(50, int(self._cfg("max_results", 27) or 27)))
        await asyncio.to_thread(self._maybe_auto_clean)
        songs = await asyncio.to_thread(self._search_all, keyword, limit)
        if not songs:
            hint = "换个关键词试试"
            if "netease" in str(self._cfg("sources", "")) or "bilibili" in str(self._cfg("sources", "")):
                hint = "换个关键词，或检查网络/配置里的源"
            yield event.plain_result(f"没搜到「{keyword}」，{hint}。")
            return
        sess = {"step": "pick", "page": 0, "songs": songs, "keyword": keyword,
                "created": time.time()}
        self._sessions[self._sess_key(event)] = sess
        self._recent_play[self._sess_key(event)] = (keyword, time.time())
        self._save_sessions()
        async for item in self._send_page(event, sess):
            yield item
        event.stop_event()

    async def cmd_library(self, event: AstrMessageEvent):
        """歌单：列出本地曲库。"""
        files = await asyncio.to_thread(self._library_files)
        if not files:
            yield event.plain_result("曲库是空的。回复一条音频发「收歌」，就能存进来。")
            return
        lines = [f"{i}. {os.path.splitext(os.path.basename(p))[0]}" for i, p in enumerate(files[:30], 1)]
        more = f"\n……还有 {len(files) - 30} 首没列" if len(files) > 30 else ""
        yield event.plain_result(f"曲库共 {len(files)} 首：\n" + "\n".join(lines) + more)
        yield event.plain_result("点「点歌 <名字>」就能发。")
        event.stop_event()

    async def cmd_collect(self, event: AstrMessageEvent):
        """收歌：回复一条语音/音频/文件，存进曲库。"""
        if not self.enabled:
            return
        src = await self._find_media_in(event)
        if not src:
            yield event.plain_result("没找到音频——回复一条语音、音频或文件，再发「收歌」。")
            return
        try:
            name = str(self._cfg("collect_name", "") or "").strip()
            if not name:
                name = time.strftime("收歌_%m%d_%H%M%S")
            name = self._safe_name(name, 50) + ".mp3"
            out = os.path.join(self._library_root(), name)
            if os.path.abspath(src) == os.path.abspath(out):
                yield event.plain_result("这就是曲库里的文件，不用再收一遍。")
                return
            mp3 = await asyncio.to_thread(self._to_mp3, src, str(self._cfg("audio_bitrate", "192k")))
            shutil.move(mp3, out)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[DeepseaRadio] 收歌失败：{exc}")
            yield event.plain_result(f"收歌失败：{type(exc).__name__}: {exc}")
            return
        secs = await asyncio.to_thread(_probe, out)
        yield event.plain_result(
            f"收好了：{name}（{_mmss(secs)}，{os.path.getsize(out) / 1048576:.1f}MB）\n"
            f"之后「点歌 {os.path.splitext(name)[0]}」就能点它。")
        event.stop_event()

    async def cmd_to_voice(self, event: AstrMessageEvent):
        """转语音：回复一条音频/文件，转成语音条。"""
        if not self.enabled:
            return
        src = await self._find_media_in(event)
        if not src:
            yield event.plain_result("没找到音频——回复一条语音、音频或文件，再发「转语音」。")
            return
        total = await asyncio.to_thread(_probe, src)
        how, limit = self._plan(event, total)
        if how == "file":
            yield event.chain_result([File(name=os.path.basename(src), file=src)])
            event.stop_event()
            return
        try:
            pieces = await asyncio.to_thread(
                self._pieces, src, total, limit if how == "segments" else 0, self._is_official(event))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[DeepseaRadio] 转语音失败：{exc}")
            yield event.plain_result(f"转换失败：{type(exc).__name__}: {exc}")
            return
        yield event.chain_result([Record.fromFileSystem(p) for p in pieces])
        event.stop_event()

    async def cmd_to_audio(self, event: AstrMessageEvent):
        """转音频：回复一条语音，转成 mp3 文件。"""
        if not self.enabled:
            return
        src = await self._find_media_in(event)
        if not src:
            yield event.plain_result("没找到音频——回复一条语音或音频，再发「转音频」。")
            return
        try:
            mp3 = await asyncio.to_thread(self._to_mp3, src, str(self._cfg("audio_bitrate", "192k")))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[DeepseaRadio] 转音频失败：{exc}")
            yield event.plain_result(f"转换失败：{type(exc).__name__}: {exc}")
            return
        name = os.path.splitext(os.path.basename(src))[0] + ".mp3"
        yield event.chain_result([File(name=name, file=mp3)])
        event.stop_event()

    # ---------------- 会话后续 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_session_reply(self, event: AstrMessageEvent):
        """接编号 / 翻页 / 选格式 / 取消。不像会话输入就原样交还给 AI。"""
        if not self.enabled:
            return
        key = self._sess_key(event)
        sess = self._sessions.get(key)
        if not sess:
            # 会话没了但刚点过歌：别装没看见，告诉用户为什么数字没反应
            if re.fullmatch(r"\d{1,2}", _norm_reply(event.message_str) or ""):
                recent = self._recent_play.get(key)
                if recent and time.time() - recent[1] <= 1800:
                    yield event.plain_result(
                        f"歌单过期或丢了（刚才搜的是「{recent[0]}」）。重新发「点歌 {recent[0]}」吧。")
                    event.stop_event()
            return
        if not self._session_alive(sess):
            self._sessions.pop(key, None)
            self._save_sessions()
            ttl = int(self._cfg("session_ttl", 600) or 600)
            yield event.plain_result(f"点歌窗口超过 {ttl // 60} 分钟，过期了。重新「点歌 <关键词>」吧。")
            event.stop_event()
            return

        text = _norm_reply(event.message_str)
        if not text:
            return
        low = text.lower()

        if low in ("取消", "退出", "算了", "不点了"):
            self._sessions.pop(key, None)
            self._save_sessions()
            yield event.plain_result("行，撤了。")
            event.stop_event()
            return

        page_size = int(self._cfg("page_size", 9) or 9)
        total_pages = max(1, int(-(-len(sess["songs"]) // page_size)))
        if low in ("下", "下一页", "n"):
            if sess["page"] + 1 < total_pages:
                sess["page"] += 1
                self._save_sessions()
                async for item in self._send_page(event, sess):
                    yield item
            else:
                yield event.plain_result("最后一页了。")
            event.stop_event()
            return
        if low in ("上", "上一页", "p"):
            if sess["page"] > 0:
                sess["page"] -= 1
                self._save_sessions()
                async for item in self._send_page(event, sess):
                    yield item
            else:
                yield event.plain_result("第一页了。")
            event.stop_event()
            return

        m = re.fullmatch(r"(\d{1,2})\s*(文件|原轨|file)?", text)
        if not m:
            return
        idx = int(m.group(1))
        songs = sess["songs"]
        if not 1 <= idx <= len(songs):
            yield event.plain_result(f"没有 {idx} 号，现在共 {len(songs)} 首。")
            event.stop_event()
            return
        sess["chosen"] = songs[idx - 1]
        fmt = "file" if m.group(2) else "voice"
        async for item in self._send_song(event, sess, fmt):
            yield item
        event.stop_event()

    # ---------------- WebUI 控制台 ----------------
    _PAGE_SETTABLE = {
        "library_dirs": str,
        "max_results": int,
        "page_size": int,
        "max_segments": int,
        "voice_max_seconds": int,
        "title_chars": int,
        "audio_bitrate": str,
        "codec": str,
        "mode": str,
        "platform_mode": str,
        "cooldown": int,
        "session_ttl": int,
        "show_covers": bool,
        "show_quality": bool,
        "save_downloaded": bool,
        "reply_hint": bool,
        "bili_music_only": bool,
        "cover_style": str,
        "auto_clean_hours": int,
        "cache_max_files": int,
    }
    _PAGE_CONFIG_KEYS = tuple(_PAGE_SETTABLE.keys())

    def _register_page_api(self) -> None:
        """把控制台能力挂到插件 Pages（WebUI 里点着用）。"""
        if json_response is None or request is None or self.context is None:
            logger.warning("[DeepseaRadio] 没有 astrbot.api.web，控制台 API 跳过")
            return
        routes = (
            (f"/{_PLUGIN_NAME}/console/state", self.page_state, ["GET"], "深海电台：状态总览"),
            (f"/{_PLUGIN_NAME}/console/library", self.page_library, ["GET"], "深海电台：曲库列表"),
            (f"/{_PLUGIN_NAME}/console/cleanup", self.page_cleanup, ["POST"], "深海电台：清理缓存"),
            (f"/{_PLUGIN_NAME}/console/log", self.page_log, ["GET"], "深海电台：日志尾巴"),
            (f"/{_PLUGIN_NAME}/console/save", self.page_save, ["POST"], "深海电台：保存设置"),
            (f"/{_PLUGIN_NAME}/console/test_card", self.page_test_card, ["POST"], "深海电台：测试卡片"),
        )
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[DeepseaRadio] 注册控制台 API 失败 {route}：{exc}")

    @staticmethod
    def _astrbot_root() -> str:
        return os.environ.get("ASTRBOT_ROOT", "/root/AstrBot")

    def _config_path(self) -> str:
        return os.path.join(self._astrbot_root(), "data", "config",
                            f"{_PLUGIN_NAME}_config.json")

    def _save_config(self) -> bool:
        """把 self.config 合并写回插件配置文件。"""
        try:
            path = self._config_path()
            disk: dict = {}
            if os.path.exists(path):
                with open(path, encoding="utf-8-sig") as fh:
                    disk = json.load(fh) or {}
            disk.update(self.config)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(disk, fh, ensure_ascii=False, indent=2)
            self.config = disk
            return True
        except (OSError, ValueError) as exc:
            logger.warning(f"[DeepseaRadio] 保存配置失败：{exc}")
            return False

    def _library_rows(self, query: str = "", probe: bool = False) -> list[dict]:
        keyword = (query or "").strip().lower()
        rows: list[dict] = []
        for folder in self._library_dirs():
            if not os.path.isdir(folder):
                continue
            for path in sorted(glob.glob(os.path.join(folder, "*"))):
                if not path.lower().endswith(AUDIO_EXT):
                    continue
                name = os.path.basename(path)
                if keyword and keyword not in name.lower():
                    continue
                try:
                    st = os.stat(path)
                    mb = round(st.st_size / 1048576, 2)
                    mtime = time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime))
                except OSError:
                    mb, mtime = 0.0, ""
                row = {"name": name, "mb": mb, "mtime": mtime, "path": path}
                if probe and len(rows) < 60:
                    row["seconds"] = round(float(self._probe_cached(path)), 1)
                rows.append(row)
        rows.sort(key=lambda r: r["name"])
        return rows

    @staticmethod
    def _dir_stat(path: str) -> tuple[float, int]:
        total, files = 0, 0
        for root, _dirs, names in os.walk(path):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                    files += 1
                except OSError:
                    pass
        return total / 1048576, files

    async def page_state(self):
        rows = await asyncio.to_thread(self._library_rows)
        total_mb = round(sum(r["mb"] for r in rows), 1)
        temp_dir = tempfile.gettempdir()
        temp_mb, temp_files = await asyncio.to_thread(self._dir_stat, self._cover_dir())
        cookie = str(self._cfg("bilibili_cookie", "") or "").strip()
        login = await asyncio.to_thread(bili_login.login_report, cookie)
        login["source"] = "B站"
        login["can_flac"] = bool(login.get("logged_in")) and login.get("vip") == "大会员"
        return json_response({
            "ok": True,
            "login": login,
            "library": {"dirs": self._library_dirs(), "count": len(rows), "total_mb": total_mb},
            "temp": {"mb": round(temp_mb, 1), "files": temp_files, "dir": temp_dir},
            "config": {k: self._cfg(k, None) for k in self._PAGE_CONFIG_KEYS},
            "limits": {"official_max_seconds": OFFICIAL_HARD_MAX, "slice": OFFICIAL_SLICE},
            "runtime": {"sessions": len(self._sessions), "cooldowns": len(self._cooldowns)},
        })

    async def page_library(self):
        query = request.query.get("q", "") or ""
        probe = str(request.query.get("probe", "0")).lower() in ("1", "true", "yes")
        try:
            page = max(0, int(request.query.get("page", 0, type=int) or 0))
            size = max(1, min(200, int(request.query.get("size", 50, type=int) or 50)))
        except (TypeError, ValueError):
            page, size = 0, 50
        rows = await asyncio.to_thread(self._library_rows, query, probe)
        start = page * size
        return json_response({
            "ok": True, "total": len(rows), "page": page, "size": size,
            "items": rows[start:start + size],
        })

    async def page_cleanup(self):
        """清自己的封面缓存 + /tmp 里的临时音频。"""
        report = []
        try:
            cover_dir = self._cover_dir()
            grids = [os.path.join(cover_dir, f) for f in os.listdir(cover_dir)]
            freed = 0
            for path in grids:
                try:
                    freed += os.path.getsize(path)
                    os.remove(path)
                except OSError:
                    pass
            report.append(f"封面/拼图缓存：清掉 {len(grids)} 个，{freed / 1048576:.1f}MB")
        except OSError as exc:
            report.append(f"清封面缓存失败：{exc}")

        tmp = tempfile.gettempdir()
        killed = 0
        try:
            for name in os.listdir(tmp):
                if name.startswith(("dsr_", "a2v_", "a2f_", "bili_qr_")):
                    try:
                        os.remove(os.path.join(tmp, name))
                        killed += 1
                    except OSError:
                        pass
            report.append(f"临时音频：清掉 {killed} 个")
        except OSError as exc:
            report.append(f"清临时目录失败：{exc}")
        return json_response({"ok": True, "report": report})

    async def page_log(self):
        try:
            lines = int(request.query.get("lines", 60, type=int) or 60)
        except (TypeError, ValueError):
            lines = 60
        lines = max(10, min(300, lines))
        path = os.path.join(self._astrbot_root(), "data", "logs", "astrbot.log")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                tail = fh.readlines()[-lines:]
        except OSError as exc:
            return error_response(f"读不到日志：{exc}")
        return json_response({"ok": True, "path": path,
                              "lines": [t.rstrip(chr(10)) for t in tail]})

    async def page_test_card(self):
        """调试用：把一张 B站卡片发到指定会话（umo），验证客户端认不认。"""
        if MessageChain is None or MessageSesion is None:
            return error_response("缺少消息链依赖")
        payload = await request.json(default={}) or {}
        umo = str(payload.get("umo") or "").strip()
        bvid = str(payload.get("bvid") or "").strip()
        if not umo or not bvid:
            return error_response("要 umo 和 bvid")
        song = await asyncio.to_thread(self._song_by_bvid, bvid)
        if not song:
            return error_response("查不到这个 BV 号")
        session = MessageSesion.from_str(umo)
        inst = self.context.get_platform_inst(session.platform_name) if self.context else None
        if not inst:
            return error_response(f"平台 {session.platform_name} 没在跑")
        try:
            await inst.send_by_session(
                session, MessageChain(chain=[RawJson(payload=json.dumps(
                    self._bili_card_payload(song), ensure_ascii=False))]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[DeepseaRadio] 测试卡片失败：{exc}")
            return error_response(f"发送失败：{exc}")
        return json_response({"ok": True, "title": song.get("name")})

    async def page_save(self):
        payload = await request.json(default={}) or {}
        if not isinstance(payload, dict):
            return error_response("请求体要是 JSON 对象")
        changed: dict = {}
        for key, value in payload.items():
            caster = self._PAGE_SETTABLE.get(key)
            if caster is None:
                continue
            try:
                cast = bool(value) if caster is bool else caster(value)
            except (TypeError, ValueError):
                return error_response(f"「{key}」的值不合法")
            self.config[key] = cast
            changed[key] = cast
        saved = self._save_config()
        return json_response({"ok": bool(saved), "changed": changed})

    async def terminate(self):
        self._sessions.clear()
        logger.info("[DeepseaRadio] 关台，鲸尾一摆下班。")
