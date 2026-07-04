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
    di gayi hon, sabko uthata hai. Koi fixed limit nahi hai — jitni
    keys add karoge utni hi use hongi.
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
            return None

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

        self.yt_api_url = os.environ.get("YT_API", "https://api.onegrab.fun").rstrip("/")
        self.yt_api_keys = YTAPIKeyManager(_load_yt_api_keys())
        self.yt_api_max_size = 60 * 1024 * 1024
        self.yt_api_timeout = aiohttp.ClientTimeout(total=20)
        self.yt_api_dl_timeout = aiohttp.ClientTimeout(total=60)

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

    async def _yt_api_resolve_track(self, youtube_url: str, want_video: bool, key: str) -> dict:
        async with aiohttp.ClientSession(timeout=self.yt_api_timeout) as session:
            async with session.get(
                f"{self.yt_api_url}/api/track",
                params={"url": youtube_url, "video": "true" if want_video else "false"},
                headers={"X-API-Key": key},
            ) as resp:
                if resp.status != 200:
                    return {"_status": resp.status}
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    return {"_status": 0}

    async def _yt_api_stream_file(self, file_id: str, filename: str, key: str) -> str | None:
        tmp_filename = filename + ".part"
        async with aiohttp.ClientSession(timeout=self.yt_api_dl_timeout) as session:
            async with session.get(
                f"{self.yt_api_url}/stream",
                params={"id": file_id},
                headers={"X-API-Key": key},
            ) as resp:
                if resp.status != 200:
                    return None

                content_type = resp.headers.get("Content-Type", "")
                if content_type and not (
                    content_type.startswith("audio/")
                    or content_type.startswith("video/")
                    or content_type == "application/octet-stream"
                ):
                    logger.warning("YT_API: unexpected content-type: %s", content_type)
                    return None

                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > self.yt_api_max_size:
                    logger.warning("YT_API: file too large (%s bytes), skipping.", content_length)
                    return None

                os.makedirs("downloads", exist_ok=True)
                written = 0
                with open(tmp_filename, "wb") as fw:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
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

    async def download_via_yt_api(self, video_id: str, filename: str, video: bool = False) -> str | None:
        logger.warning("YT_API: keys loaded = %d", len(self.yt_api_keys.keys))
        if not self.yt_api_keys.keys:
            logger.warning("YT_API: no keys found, skipping to yt-dlp fallback.")
            return None

        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            logger.warning("YT_API: invalid video_id format, skipping.")
            return None

        key = await self.yt_api_keys.get_key()
        if not key:
            logger.warning("YT_API: all API keys exhausted for today.")
            return None

        youtube_url = self.base + video_id

        try:
            track = await self._yt_api_resolve_track(youtube_url, video, key)

            if track.get("_status") in (429, 403):
                await self.yt_api_keys.mark_exhausted(key)
                return await self.download_via_yt_api(video_id, filename, video)
            if "_status" in track:
                logger.warning("YT_API: request failed, response: %s", track)
                return None

            file_id = track.get("id")
            if not file_id:
                logger.warning("YT_API: response mein 'id' nahi mila: %s", track)
                return None

            return await self._yt_api_stream_file(file_id, filename, key)
        except asyncio.TimeoutError:
            logger.warning("YT_API: request timed out.")
            return None
        except Exception as ex:
            logger.warning("YT_API download failed: %s", ex)
            return None

    async def download(self, video_id: str, video: bool = False) -> str | None:
        url = self.base + video_id
        ext = "mp4" if video else "webm"
        filename = f"downloads/{video_id}.{ext}"

        if Path(filename).exists():
            return filename

        result = await self.download_via_yt_api(video_id, filename, video)
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
            return filename

        return await asyncio.to_thread(_download)
