import asyncio
import math
import posixpath
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from .auth import COOKIE, IDP, CoursesError

MAX_FILE = 20 * 1024**2


def local(url):
    try:
        u = urlsplit(url)
        return (
            u.scheme == "https"
            and u.netloc == "courses.fit.cvut.cz"
            and "\\" not in url
        )
    except ValueError:
        return False


def within(url, root):
    if not local(url) or not local(root):
        return False
    path = urlsplit(url).path
    decoded = unquote(path)
    if (
        "%" in decoded
        or "\\" in decoded
        or any(s in (".", "..") for s in decoded.split("/"))
    ):
        return False
    root_path = urlsplit(root).path
    if not root_path.endswith("/") and not posixpath.splitext(root_path)[1]:
        prefix = root_path + "/"
        if decoded == root_path:
            return True
    else:
        prefix = urlsplit(urljoin(root, ".")).path
    return decoded.startswith(unquote(prefix)) and not any(
        s.startswith("@") for s in decoded[len(unquote(prefix)) :].split("/")
    )


@dataclass
class Document:
    url: str
    content: bytes
    media_type: str
    size: int | None


class LoginNeeded(Exception):
    pass


class Portal:
    def __init__(self, auth):
        self.auth = auth
        self.client = httpx.AsyncClient(timeout=15)
        self.slots = asyncio.Semaphore(2)
        self.retry_at = 0.0

    def check_cooldown(self):
        remaining = math.ceil(self.retry_at - time.monotonic())
        if remaining > 0:
            raise CoursesError(f"RATE_LIMITED: Retry in {remaining} seconds.")

    async def _fetch(self, url, root, token, method, limit):
        async with self.slots, asyncio.timeout(40):
            for _ in range(7):
                self.check_cooldown()
                if not within(url, root):
                    raise CoursesError(
                        "OUTSIDE_COURSE: Redirect left the course website."
                    )
                headers = {"User-Agent": "fit-courses-mcp/0.1"}
                if token:
                    headers["Cookie"] = f"{COOKIE}={token}"
                request = httpx.Request(
                    method,
                    url,
                    headers=headers,
                    extensions={"timeout": self.client.timeout.as_dict()},
                )
                response = await self.client.send(request, stream=True)
                try:
                    status = response.status_code
                    if response.is_redirect:
                        target = urljoin(url, response.headers.get("location", ""))
                        if target.startswith(IDP + "/"):
                            raise LoginNeeded()
                        url = target
                        continue
                    if status == 401:
                        raise LoginNeeded()
                    if status == 429 or (
                        status == 503 and "retry-after" in response.headers
                    ):
                        retry = response.headers.get("retry-after", "1")
                        try:
                            seconds = (
                                int(retry)
                                if retry.isdigit()
                                else parsedate_to_datetime(retry).timestamp()
                                - time.time()
                            )
                        except (ValueError, OverflowError, TypeError):
                            seconds = 1
                        self.retry_at = max(
                            self.retry_at, time.monotonic() + max(1, seconds)
                        )
                        self.check_cooldown()
                    if status >= 400:
                        label = {403: "FORBIDDEN", 404: "NOT_FOUND"}.get(
                            status, "UPSTREAM_ERROR"
                        )
                        raise CoursesError(f"{label}: Courses returned HTTP {status}.")
                    length = response.headers.get("content-length", "")
                    size = int(length) if length.isdecimal() else None
                    media = (
                        response.headers.get("content-type", "").split(";")[0].lower()
                    )
                    data = bytearray()
                    if method != "HEAD":
                        if size and size > limit:
                            raise CoursesError(
                                "TOO_LARGE: The file exceeds the download limit."
                            )
                        async for chunk in response.aiter_bytes(65536):
                            if len(data) + len(chunk) > limit:
                                raise CoursesError(
                                    "TOO_LARGE: The file exceeds the download limit."
                                )
                            data.extend(chunk)
                        if (
                            media == "text/html"
                            and b"j_password" in data
                            and b"j_username" in data
                            and b"<form" in data
                        ):
                            raise LoginNeeded()
                    return Document(url, bytes(data), media, size)
                finally:
                    await response.aclose()
            raise CoursesError("UPSTREAM_ERROR: Too many redirects.")

    async def fetch(self, url, root, ctx=None, method="GET", limit=4 * 1024**2):
        try:
            try:
                return await self._fetch(url, root, None, method, limit)
            except LoginNeeded:
                if ctx is None:
                    raise CoursesError("AUTH_REQUIRED: Expected public material.")
            generation = self.auth.generation
            token = self.auth.token
            if token:
                try:
                    doc = await self._fetch(url, root, token, method, limit)
                    self.auth.outcome = "verified"
                    return doc
                except LoginNeeded:
                    pass
            await self.auth.sign_in(ctx, generation)
            try:
                doc = await self._fetch(url, root, self.auth.token, method, limit)
                self.auth.outcome = "verified"
                return doc
            except LoginNeeded:
                self.auth.outcome = "login_failed"
                raise CoursesError(
                    "AUTH_FAILED: FIT still requests login for this resource."
                ) from None
        except (httpx.HTTPError, TimeoutError):
            raise CoursesError(
                "NETWORK_ERROR: Could not complete the Courses request."
            ) from None
