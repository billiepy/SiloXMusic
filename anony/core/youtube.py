# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import os
import re
import yt_dlp
import random
import asyncio
import aiohttp
from pathlib import Path

from py_yt import Playlist, VideosSearch

from anony import logger
from anony.helpers import Track, utils


def _load_yt_api_keys() -> list[str]:
    """
    .env se YT_API_KEY_1, YT_API_KEY_2, YT_API_KEY_3 ... jitni bhi keys
    di gayi hon, sabko uthata hai. Koi fixed limit nahi hai.
    """
    keys = []
    idx = 1
    while True:
        key = os.environ.get(f"YT_API_KEY_{idx}")
        if not key:
            break
        keys.append(key)
        idx += 1
    return keys


class YTAPIKeyManager:
    def __init__(self, keys: list[str]):
        self.keys = keys
        self.exhausted = {k: False for k in keys}
        self._idx = 0
        self._lock = asyncio.Lock()

    async def get_key(self) -> str | None:
        async with self._lock:
            for _ in range(len(self.keys)):
                if not self.keys:
                    return None
                key = self.keys[self._idx % len(self.keys)]
                self._idx += 1
                if not self.exhausted[key]:
                    return key
            return None  # sab keys ka daily limit khatam ho gaya

    async def mark_exhausted(self, key: str):
        async with self._lock:
            self.exhausted[key] = True

    async def reset(self):
        async with self._lock:
            self.exhausted = {k: False for k in self.keys}


class YouTube:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.cookies = []
        self.checked = False
        self.cookie_dir = "anony/cookies"
        self.warned = False
        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)([&?][^\s]*)?"
        )
        self.iregex = re.compile(
            r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)"
            r"(?!/(watch\?v=[A-Za-z0-9_-]{11}|shorts/[A-Za-z0-9_-]{11}"
            r"|playlist\?list=PL[A-Za-z0-9_-]+|[A-Za-z0-9_-]{11}))\S*"
        )

        # ---- ShrutiBots third-party API (unlimited key rotation) ----
        self.yt_api_url = os.environ.get("YT_API", "https://api.shrutibots.site").rstrip("/")
        self.yt_api_keys = YTAPIKeyManager(_load_yt_api_keys())
        self.yt_api_max_size = 60 * 1024 * 1024  # 60MB cap
        self.yt_api_dl_timeout_audio = aiohttp.ClientTimeout(total=120)
        self.yt_api_dl_timeout_video = aiohttp.ClientTimeout(total=300)

    def get_cookies(self):
        if not self.checked:
            for file in os.listdir(self.cookie_dir):
                if file.endswith(".txt"):
                    self.cookies.append(f"{self.cookie_dir}/{file}")
            self.checked = True
        if not self.cookies:
            if not self.warned:
                self.warned = True
                logger.warning("Cookies are missing; downloads might fail.")
            return None
        return random.choice(self.cookies)

    async def save_cookies(self, urls: list[str]) -> None:
        logger.info("Saving cookies from urls...")
        async with aiohttp.ClientSession() as session:
            for url in urls:
                name = url.split("/")[-1]
                link = "https://batbin.me/raw/" + name
                async with session.get(link) as resp:
                    resp.raise_for_status()
                    with open(f"{self.cookie_dir}/{name}.txt", "wb") as fw:
                        fw.write(await resp.read())
        logger.info(f"Cookies saved in {self.cookie_dir}.")

    def valid(self, url: str) -> bool:
        return bool(re.match(self.regex, url))

    def invalid(self, url: str) -> bool:
        return bool(re.match(self.iregex, url))

    async def search(self, query: str, m_id: int, video: bool = False) -> Track | None:
        try:
            _search = VideosSearch(query, limit=1, with_live=False)
            results = await _search.next()
        except Exception:
            return None
        if results and results["result"]:
            data = results["result"][0]
            return Track(
                id=data.get("id"),
                channel_name=data.get("channel", {}).get("name"),
                duration=data.get("duration"),
                duration_sec=utils.to_seconds(data.get("duration")),
                message_id=m_id,
                title=data.get("title")[:25],
                thumbnail=data.get("thumbnails", [{}])[-1].get("url").split("?")[0],
                url=data.get("link"),
                view_count=data.get("viewCount", {}).get("short"),
                video=video,
            )
        return None

    async def playlist(self, limit: int, user: str, url: str, video: bool) -> list[Track | None]:
        tracks = []
        try:
            plist = await Playlist.get(url)
            for data in plist["videos"][:limit]:
                track = Track(
                    id=data.get("id"),
                    channel_name=data.get("channel", {}).get("name", ""),
                    duration=data.get("duration"),
                    duration_sec=utils.to_seconds(data.get("duration")),
                    title=data.get("title")[:25],
                    thumbnail=data.get("thumbnails")[-1].get("url").split("?")[0],
                    url=data.get("link").split("&list=")[0],
                    user=user,
                    view_count="",
                    video=video,
                )
                tracks.append(track)
        except Exception:
            pass
        return tracks

    async def download_via_yt_api(self, video_id: str, filename: str, video: bool = False) -> str | None:
        if not self.yt_api_keys.keys:
            return None

        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            logger.warning("YT_API: invalid video_id format, skipping.")
            return None

        key = await self.yt_api_keys.get_key()
        if not key:
            logger.warning("YT_API: all API keys exhausted for today.")
            return None

        media_type = "video" if video else "audio"
        timeout = self.yt_api_dl_timeout_video if video else self.yt_api_dl_timeout_audio
        tmp_filename = filename + ".part"

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self.yt_api_url}/download",
                    params={"url": video_id, "type": media_type, "api_key": key},
                ) as resp:
                    if resp.status in (429, 403):
                        await self.yt_api_keys.mark_exhausted(key)
                        return await self.download_via_yt_api(video_id, filename, video)

                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            "YT_API: /download returned status %s, body=%s",
                            resp.status, body[:300],
                        )
                        return None

                    content_type = resp.headers.get("Content-Type", "")
                    if content_type and not (
                        content_type.startswith("audio/")
                        or content_type.startswith("video/")
                        or content_type == "application/octet-stream"
                    ):
                        body = await resp.text()
                        logger.warning(
                            "YT_API: unexpected content-type %s, body=%s",
                            content_type, body[:300],
                        )
                        return None

                    content_length = resp.headers.get("Content-Length")
                    if content_length and int(content_length) > self.yt_api_max_size:
                        logger.warning("YT_API: file too large (%s bytes), skipping.", content_length)
                        return None

                    os.makedirs("downloads", exist_ok=True)
                    written = 0
                    with open(tmp_filename, "wb") as fw:
                        async for chunk in resp.content.iter_chunked(131072):
                            written += len(chunk)
                            if written > self.yt_api_max_size:
                                logger.warning("YT_API: download exceeded size cap mid-stream, aborting.")
                                fw.close()
                                os.remove(tmp_filename)
                                return None
                            fw.write(chunk)

            if os.path.exists(tmp_filename) and os.path.getsize(tmp_filename) > 0:
                os.replace(tmp_filename, filename)
                return filename
            if os.path.exists(tmp_filename):
                os.remove(tmp_filename)
            return None
        except asyncio.TimeoutError:
            logger.warning("YT_API: request timed out.")
            if os.path.exists(tmp_filename):
                os.remove(tmp_filename)
            return None
        except Exception as ex:
            logger.warning("YT_API download failed: %s", ex)
            if os.path.exists(tmp_filename):
                try:
                    os.remove(tmp_filename)
                except Exception:
                    pass
            return None

    async def download(self, video_id: str, video: bool = False) -> str | None:
        url = self.base + video_id

        # ShrutiBots audio ko mp3 deta hai, yt-dlp fallback webm deta hai —
        # dono extensions alag cache check karte hain taaki koi conflict na ho
        ext_api = "mp4" if video else "mp3"
        ext_ytdlp = "mp4" if video else "webm"
        filename_api = f"downloads/{video_id}.{ext_api}"
        filename_ytdlp = f"downloads/{video_id}.{ext_ytdlp}"

        if Path(filename_api).exists():
            return filename_api
        if Path(filename_ytdlp).exists():
            return filename_ytdlp

        result = await self.download_via_yt_api(video_id, filename_api, video)
        if result:
            return result

        cookie = self.get_cookies()
        base_opts = {
            "outtmpl": "downloads/%(id)s.%(ext)s",
            "quiet": True,
            "noplaylist": True,
            "geo_bypass": True,
            "no_warnings": True,
            "overwrites": False,
            "nocheckcertificate": True,
            "cookiefile": cookie,
        }

        if video:
            ydl_opts = {
                **base_opts,
                "format": "(bestvideo[height<=?720][width<=?1280][ext=mp4])+(bestaudio)",
                "merge_output_format": "mp4",
            }
        else:
            ydl_opts = {
                **base_opts,
                "format": "bestaudio[ext=webm][acodec=opus]",
            }

        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    ydl.download([url])
                except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError):
                    return None
                except Exception as ex:
                    logger.warning("Download failed: %s", ex)
                    return None
            return filename_ytdlp

        return await asyncio.to_thread(_download)
            
