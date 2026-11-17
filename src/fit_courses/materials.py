import asyncio
import json
import mimetypes
import posixpath
import re
import sys
import time
import unicodedata
from collections import deque
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup
from markdownify import markdownify
from playwright.async_api import Error as BrowserError

from .auth import ORIGIN, CoursesError, semesters
from .http import MAX_FILE, local, within


def fold(text):
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(c)
    )


def batch(rows, cursor, limit):
    if cursor < 0:
        raise CoursesError("INVALID_CURSOR: Use a nonnegative cursor.")
    return {
        "results": rows[cursor : cursor + limit],
        "next_cursor": cursor + limit if cursor + limit < len(rows) else None,
    }


def link_kind(url, root):
    if not within(url, root):
        return "link"
    ext = posixpath.splitext(urlsplit(url).path)[1].lower()
    return "page" if ext in ("", ".html", ".htm", ".xhtml") else "attachment"


def parse(doc, root):
    if doc.media_type not in ("text/html", "application/xhtml+xml"):
        raise CoursesError("UNSUPPORTED_PAGE: The link did not return HTML.")
    soup = BeautifulSoup(doc.content, "html.parser", from_encoding="utf-8")
    title = soup.title.get_text(" ", strip=True) if soup.title else doc.url
    for node in soup.select(
        "script, style, svg, .hidden, [hidden], .anchor, .nav-link, .display-menu"
    ):
        node.decompose()
    nav = soup.select_one("nav#nav, nav.site-nav")
    main = soup.select_one("main, article") or soup.body or soup

    def links(parent, navigation=False):
        result = {}
        for a in parent.select("a[href]"):
            href = a.get("href", "")
            if not href or href.startswith("#"):
                continue
            try:
                url = urljoin(doc.url, href)
                parts = urlsplit(url)
                if (
                    parts.scheme not in ("http", "https")
                    or not parts.hostname
                    or parts.username
                    or parts.password
                ):
                    continue
                url = urldefrag(url).url if navigation else url
                kind = link_kind(url, root)
            except ValueError:
                continue
            label = (
                a.get_text(" ", strip=True)
                or a.get("title")
                or unquote(parts.path.rsplit("/", 1)[-1])
                or url
            )
            row = {"kind": kind, "title": label, "url": url}
            if navigation:
                ancestors = []
                for li in a.find_parents("li")[1:]:
                    group = li.find("a", recursive=False)
                    if group:
                        ancestors.append(group.get_text(" ", strip=True))
                row["breadcrumb"] = [*reversed(ancestors), label]
            result.setdefault(url, row)
            a["href"] = url
        return list(result.values())

    navigation = links(nav, True) if nav else []
    for node in main.select("nav, header.site-header, footer.site-footer"):
        node.decompose()
    body_links = links(main)
    for element in main.select("math[alttext]"):
        element.replace_with(element["alttext"])
    title_node = main.find("h1")
    return (
        title_node.get_text(" ", strip=True) if title_node else title,
        navigation,
        body_links,
        main,
    )


class Materials:
    def __init__(self, portal):
        self.portal = portal
        self.catalogues = {}
        self.archives = None
        self.refs = {}
        self.indexes = {}
        self.generation = 0

    def sync_identity(self):
        if self.portal.auth.generation != self.generation:
            self.refs.clear()
            self.indexes.clear()
            self.generation = self.portal.auth.generation

    async def catalogue(self, snapshot):
        if snapshot != "all" and not re.fullmatch(r"B\d{3}", snapshot):
            raise CoursesError("INVALID_SNAPSHOT: Use all or a semester such as B232.")
        url = f"{ORIGIN}/data/courses-{snapshot}.json"
        cached = self.catalogues.get(snapshot)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        doc = await self.portal.fetch(url, url)
        try:
            data = json.loads(doc.content)
            if not isinstance(data["courses"], dict):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise CoursesError(
                "CATALOGUE_ERROR: Courses returned an unfamiliar catalogue."
            ) from None
        self.catalogues[snapshot] = time.monotonic() + 300, data
        return data

    async def snapshots(self):
        data = await self.catalogue("all")
        if not self.archives or time.monotonic() >= self.archives[0]:
            try:
                choices = await semesters()
                self.archives = time.monotonic() + 3600, choices
            except BrowserError:
                return {
                    "default": "all",
                    "current_semester": data.get("semester"),
                    "archives": [],
                    "complete": False,
                }
        return {
            "default": "all",
            "current_semester": data.get("semester"),
            "archives": self.archives[1],
            "complete": True,
        }

    async def find(self, query, snapshot, active, language, cursor):
        data = await self.catalogue(snapshot)
        needle = fold(query.strip())
        matches = []
        for code, info in data["courses"].items():
            if active is not None and info.get("active") is not active:
                continue
            if (
                language
                and language.casefold() != (info.get("classesLang") or "").casefold()
            ):
                continue
            name = fold(" ".join(info.get(k) or "" for k in ("nameCs", "nameEn")))
            key = fold(code)
            if needle not in key and needle not in name:
                continue
            home = info.get("homepage")
            item = {
                k: info.get(k)
                for k in (
                    "nameCs",
                    "nameEn",
                    "classesLang",
                    "department",
                    "season",
                    "programmeType",
                    "active",
                )
            }
            item.update(
                code=code,
                snapshot=snapshot,
                homepage=home,
                homepage_state="absent"
                if not home
                else "courses"
                if local(home)
                else "external",
            )
            matches.append(
                (0 if key == needle else 1 if key.startswith(needle) else 2, code, item)
            )
        return {
            "source": f"{ORIGIN}/data/courses-{snapshot}.json",
            **batch([row for _, _, row in sorted(matches)], cursor, 50),
        }

    async def home(self, snapshot, code):
        data = await self.catalogue(snapshot)
        if code not in data["courses"]:
            raise CoursesError(
                "UNKNOWN_COURSE: Select an exact code from find_courses."
            )
        home = data["courses"][code].get("homepage")
        if not home or not local(home):
            raise CoursesError(
                "NO_COURSES_SITE: See the homepage field in find_courses."
            )
        return home

    async def load(self, root, url, ctx):
        doc = await self.portal.fetch(url, root, ctx)
        title, nav, links, main = parse(doc, root)
        self.sync_identity()
        refs = self.refs.setdefault(root, {})
        refs[urldefrag(doc.url).url] = "page"
        for row in nav + links:
            if row["kind"] != "link":
                refs[urldefrag(row["url"]).url] = row["kind"]
        return title, nav, links, main, doc.url

    async def pages(self, code, snapshot, query, cursor, ctx):
        root = await self.home(snapshot, code)
        title, nav, links, _, _ = await self.load(root, root, ctx)
        cache = self.indexes.get(root)
        if not cache or time.monotonic() >= cache[0]:
            rows = {
                root: {
                    "url": root,
                    "title": title,
                    "breadcrumb": [title],
                    "kind": "page",
                }
            }
            queue = deque()
            visited = {root}

            def add(rows_to_add, parents):
                for r in rows_to_add:
                    url = urldefrag(r["url"]).url
                    if r["kind"] == "page" and url not in rows:
                        rows[url] = dict(
                            r,
                            url=url,
                            breadcrumb=r.get("breadcrumb") or [*parents, r["title"]],
                        )
                        queue.append(url)

            add(nav, [])
            add(links, [title])
            deadline = time.monotonic() + 8
            partial = False
            while queue and len(visited) < 100 and time.monotonic() < deadline:
                url = queue.popleft()
                visited.add(url)
                try:
                    async with asyncio.timeout(max(0.01, deadline - time.monotonic())):
                        _, nested, related, _, _ = await self.load(root, url, ctx)
                except (TimeoutError, CoursesError):
                    partial = True
                    break
                add(nested, [])
                add(related, rows[url]["breadcrumb"])
            cache = time.monotonic() + 120, list(rows.values()), not (partial or queue)
            self.indexes[root] = cache
        matches = [r for r in cache[1] if fold(query) in fold(r["title"])]
        return {"source": root, "complete": cache[2], **batch(matches, cursor, 50)}

    async def reference(self, snapshot, code, url, kind):
        root = await self.home(snapshot, code)
        target = urldefrag(url or root).url
        if (
            not (kind == "page" and target == root)
            and self.refs.get(root, {}).get(target) != kind
        ):
            raise CoursesError(
                "UNKNOWN_REFERENCE: First discover this URL with list_pages or read_page in this session."
            )
        return root, target

    async def read(self, code, snapshot, url, heading, offset, ctx):
        root, target = await self.reference(snapshot, code, url, "page")
        title, nav, links, main, source = await self.load(root, target, ctx)
        text = markdownify(str(main), heading_style="ATX", bullets="-").strip()
        heading = heading or unquote(urlsplit(url or "").fragment)
        if heading:
            node = main.find(id=heading)
            if node and node.name in {f"h{i}" for i in range(1, 7)}:
                heading = node.get_text(" ", strip=True)
            match = next(
                (
                    m
                    for m in re.finditer(r"^(#{1,6}) (.+)$", text, re.MULTILINE)
                    if fold(m[2].strip()) == fold(heading)
                ),
                None,
            )
            if not match:
                raise CoursesError(
                    "UNKNOWN_HEADING: Read the page without a fragment to see available headings."
                )
            rest = text[match.end() :]
            stop = re.search(r"^#{1," + str(len(match[1])) + r"} ", rest, re.MULTILINE)
            text = text[
                match.start() : match.end() + (stop.start() if stop else len(rest))
            ]
        if offset < 0 or offset > len(text):
            raise CoursesError("INVALID_OFFSET: Offset is outside the page.")
        return {
            "source": source,
            "title": title,
            "text": text[offset : offset + 16000],
            "next_offset": offset + 16000 if offset + 16000 < len(text) else None,
            "links": links,
            "navigation": nav,
        }

    async def attachment(self, code, snapshot, url, start, end, offset, ctx):
        root, target = await self.reference(snapshot, code, url, "attachment")
        if start < 1 or end is not None and end < start or offset < 0:
            raise CoursesError(
                "INVALID_RANGE: Use one-based page numbers and a nonnegative text offset."
            )
        if urlsplit(target).path.lower().endswith(".pdf"):
            doc = await self.portal.fetch(target, root, ctx, limit=MAX_FILE)
        else:
            doc = await self.portal.fetch(target, root, ctx, method="HEAD")
            if doc.media_type == "application/pdf":
                doc = await self.portal.fetch(target, root, ctx, limit=MAX_FILE)
        name = unquote(urlsplit(doc.url).path.split("/")[-1])
        result = {
            "source": doc.url,
            "filename": name,
            "media_type": doc.media_type or mimetypes.guess_type(name)[0],
            "bytes": doc.size,
        }
        if doc.content.startswith(b"%PDF-"):
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "fit_courses.pdf",
                str(start),
                str(end or start + 9),
                str(offset),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                async with asyncio.timeout(20):
                    out, _ = await proc.communicate(doc.content)
            except TimeoutError:
                raise CoursesError("PDF_ERROR: PDF extraction timed out.") from None
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            if proc.returncode:
                raise CoursesError(
                    "PDF_ERROR: Unable to extract this PDF within the worker limits."
                )
            extracted = json.loads(out)
            if "error" in extracted:
                raise CoursesError(extracted["error"])
            result.update(extracted)
        elif doc.media_type == "application/pdf" or name.lower().endswith(".pdf"):
            raise CoursesError("PDF_ERROR: Missing PDF header.")
        return result
