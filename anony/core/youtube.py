# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import os
import re
import yt_dlp
import random
import asyncio
import aiohttp
import ipaddress
import socket
from urllib.parse import urlparse
from pathlib import Path

from py_yt import Playlist, VideosSearch

from anony import logger
from anony.helpers import Track, utils


class OneGrabKeyManager:
    def __init__(self, keys: list[str]):
        self.keys = [k for k in keys if k]
        self.exhausted = {k: False for k in self.keys}
        self._idx = 0
        self._lock = asyncio.Lock()

    async def get_key(self) -> str | None:
        async with self._lock:
            for _ in range(len(self.keys)):
                key = self.keys[self._idx % len(self.keys)]
                self._idx += 1
                if not self.exhausted[key]:
                    return key
            return None  # sab keys exhaust ho gayi (daily limit)

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

        # ---- OneGrab third-party API (multi-key rotation) ----
        self.onegrab_url = os.environ.get("YT_API", "https://api.onegrab.fun").rstrip("/")
        self.onegrab_keys = OneGrabKeyManager([
            os.environ.get("YT_API_KEY_1"),
            os.environ.get("YT_API_KEY_2"),
        ])
        self.onegrab_max_size = 60 * 1024 * 1024  # 60MB cap, gaana itna bada nahi hota
        self.onegrab_timeout = aiohttp.ClientTimeout(total=15)
        self.onegrab_dl_timeout = aiohttp.ClientTimeout(total=30)

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

    def _is_safe_download_url(self, link: str) -> bool:
        """SSRF guard: link https:// hona chahiye aur kisi internal/private/local IP ki taraf point nahi karna chahiye."""
        try:
            parsed = urlparse(link)
            if parsed.scheme != "https":
                return False
            host = parsed.hostname
            if not host:
                return False
            if host in ("localhost",):
                return False
            try:
                infos = socket.getaddrinfo(host, None)
            except socket.gaierror:
                return False
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_multicast
                ):
                    return False
            return True
        except Exception:
            return False

    async def download_via_onegrab(self, video_id: str, filename: str) -> str | None:
        if not self.onegrab_keys.keys:
            return None

        # video_id already yt-dlp/py_yt se controlled hota hai (11-char id), phir bhi safety ke liye validate
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            logger.warning("OneGrab: invalid video_id format, skipping.")
            return None

        key = await self.onegrab_keys.get_key()
        if not key:
            logger.warning("OneGrab: all API keys exhausted for today.")
            return None

        api_endpoint = f"{self.onegrab_url}/song/{video_id}?key={key}"

        try:
            async with aiohttp.ClientSession(timeout=self.onegrab_timeout) as session:
                async with session.get(api_endpoint) as resp:
                    if resp.status in (429, 403):
                        # is key ka quota khatam / blocked, next key try karo
                        await self.onegrab_keys.mark_exhausted(key)
                        return await self.download_via_onegrab(video_id, filename)
                    if resp.status != 200:
                        return None
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        logger.warning("OneGrab: response valid JSON nahi tha.")
                        return None

                # TODO: curl test se confirm karke field name yahan sahi karo
                dl_link = data.get("link") or data.get("url") or data.get("download_url")
                if not dl_link or not isinstance(dl_link, str):
                    logger.warning("OneGrab: response mein download link nahi mila: %s", data)
                    return None

                if not self._is_safe_download_url(dl_link):
                    logger.warning("OneGrab: unsafe/suspicious download link block kiya: %s", dl_link)
                    return None

                os.makedirs("downloads", exist_ok=True)
                tmp_filename = filename + ".part"

                async with aiohttp.ClientSession(timeout=self.onegrab_dl_timeout) as session:
                    async with session.get(dl_link) as file_resp:
                        if file_resp.status != 200:
                            return None

                        content_type = file_resp.headers.get("Content-Type", "")
                        if content_type and not (
                            content_type.startswith("audio/")
                            or content_type.startswith("video/")
                            or content_type == "application/octet-stream"
                        ):
                            logger.warning("OneGrab: unexpected content-type: %s", content_type)
                            return None

                        content_length = file_resp.headers.get("Content-Length")
                        if content_length and int(content_length) > self.onegrab_max_size:
                            logger.warning("OneGrab: file too large (%s bytes), skipping.", content_length)
                            return None

                        written = 0
                        with open(tmp_filename, "wb") as fw:
                            async for chunk in file_resp.content.iter_chunked(64 * 1024):
                                written += len(chunk)
                                if written > self.onegrab_max_size:
                                    logger.warning("OneGrab: download exceeded size cap mid-stream, aborting.")
                                    fw.close()
                                    os.remove(tmp_filename)
                                    return None
                                fw.write(chunk)

                os.replace(tmp_filename, filename)
                return filename
        except asyncio.TimeoutError:
            logger.warning("OneGrab: request timed out.")
            return None
        except Exception as ex:
            logger.warning("OneGrab download failed: %s", ex)
            return None

    async def download(self, video_id: str, video: bool = False) -> str | None:
        url = self.base + video_id
        ext = "mp4" if video else "webm"
        filename = f"downloads/{video_id}.{ext}"

        if Path(filename).exists():
            return filename

        # audio ke liye pehle OneGrab try karo (video ke liye abhi yt-dlp hi rahega)
        if not video:
            result = await self.download_via_onegrab(video_id, filename)
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
            
