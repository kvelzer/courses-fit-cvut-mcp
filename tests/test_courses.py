import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pypdf import PdfWriter

from fit_courses import auth as auth_module
from fit_courses.auth import IDP, ORIGIN, Authentication, CoursesError
from fit_courses.http import Document, Portal, within
from fit_courses.materials import Materials, parse
from fit_courses.pdf import extract

ROOT = ORIGIN + "/BI-DEMO/@B232/"


def test_course_boundaries():
    assert within(ROOT + "lecture/a.html", ROOT)
    for url in (
        ORIGIN + "/BI-DEMO/",
        ROOT + "%2e%2e/file",
        "https://outside.example/a",
        ROOT + "@B241/index.html",
        ROOT + "%252e%252e/secret",
    ):
        assert not within(url, ROOT)
    assert not within(ORIGIN + "/other/", ORIGIN + "/demo")


def test_parse_navigation_and_fallback():
    page = b"""<html><title>Title</title><body><nav id="nav"><ul><li><a href="unit/">Unit</a>
    <ul><li><a href="unit/week.html#top">Week</a></li></ul></li></ul></nav>
    <h1>Overview</h1><p><a href="lab.pdf">Lab</a> <a href="https://example.org/">Moodle</a></p>
    <script>hidden()</script></body></html>"""
    title, nav, links, main = parse(Document(ROOT, page, "text/html", len(page)), ROOT)
    assert title == "Overview"
    assert nav[-1]["breadcrumb"] == ["Unit", "Week"]
    assert nav[-1]["url"].endswith("/week.html")
    assert [r["kind"] for r in links] == ["attachment", "link"]
    assert not main.select("nav,script")


@pytest.mark.asyncio
async def test_catalogue_metadata_and_reference_provenance():
    site = Materials(None)
    site.catalogue = AsyncMock(
        return_value={
            "courses": {
                "BI-X.1": {"nameCs": "Přednášky", "homepage": ROOT},
                "BI-Y": {
                    "homepage": "https://moodle.example/",
                    "classesLang": "cs",
                    "active": True,
                },
                "BI-Z": {},
            }
        }
    )
    rows = (await site.find("", "all", None, None, 0))["results"]
    assert [r["homepage_state"] for r in rows] == ["courses", "external", "absent"]
    assert (await site.find("prednasky", "all", None, None, 0))["results"] == [rows[0]]
    assert (await site.find("", "all", True, "cs", 0))["results"] == [rows[1]]
    with pytest.raises(CoursesError, match="UNKNOWN_REFERENCE"):
        await site.reference("all", "BI-X.1", ROOT + "private.pdf", "attachment")


def context(action="accept"):
    capability = SimpleNamespace(form={}, url=None)
    ctx = SimpleNamespace(
        session=SimpleNamespace(
            client_params=SimpleNamespace(
                capabilities=SimpleNamespace(elicitation=capability)
            )
        )
    )
    ctx.elicit = AsyncMock(
        return_value=SimpleNamespace(action=action, data=SimpleNamespace(approve=True))
    )
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["accept", "decline", "cancel"])
async def test_cookie_expiry_and_session_consent(monkeypatch, decision):
    config = {"COURSES_OAUTH_ACCESS_TOKEN": "expired"}
    auth = Authentication(config)
    ctx = context(decision)
    login = AsyncMock(side_effect=["fresh1", "fresh2"])
    monkeypatch.setattr(auth_module, "browser_login", login)
    if decision == "accept":
        await asyncio.gather(auth.sign_in(ctx, 0), auth.sign_in(ctx, 0))
        await auth.sign_in(ctx, 1)
        assert login.await_count == 2 and auth.token == "fresh2"
    else:
        for _ in range(2):
            with pytest.raises(CoursesError, match="DECLINED"):
                await auth.sign_in(ctx, 0)
        login.assert_not_awaited()
    ctx.elicit.assert_awaited_once()
    assert Authentication(config).approval == "unasked"


@pytest.mark.asyncio
async def test_failed_login_and_public_read(monkeypatch):
    auth = Authentication({"COURSES_USERNAME": "test", "COURSES_PASSWORD": "fixture"})
    login = AsyncMock(side_effect=CoursesError("AUTH_FAILED: fixture"))
    monkeypatch.setattr(auth_module, "browser_login", login)
    for _ in range(2):
        with pytest.raises(CoursesError, match="AUTH_FAILED"):
            await auth.sign_in(context(), 0)
    login.assert_awaited_once()


@pytest.mark.asyncio
async def test_redirect_status_limits_and_cookie_scope():
    auth = Authentication({"COURSES_OAUTH_ACCESS_TOKEN": "fixture"})
    portal = Portal(auth)
    await portal.client.aclose()

    def respond(request):
        assert request.url.host == "courses.fit.cvut.cz"
        cookie = request.headers.get("cookie")
        path = request.url.path
        if path.endswith("/public"):
            assert not cookie
            return httpx.Response(200, text="public")
        if path.endswith("/protected"):
            if not cookie:
                return httpx.Response(303, headers={"location": IDP + "/login.html"})
            assert cookie == "oauth_access_token=fixture"
            return httpx.Response(200, text="protected")
        if path.endswith("/outside"):
            return httpx.Response(302, headers={"location": "https://outside.example/"})
        if path.endswith("/large"):
            return httpx.Response(200, content=b"1234")
        if path.endswith("/missing"):
            return httpx.Response(404)
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        portal.client = client
        assert (
            await portal.fetch(ROOT + "protected", ROOT, context())
        ).content == b"protected"
        assert auth.outcome == "verified"
        assert (
            await portal.fetch(ROOT + "public", ROOT, context())
        ).content == b"public"
        for path, error in [
            ("outside", "OUTSIDE_COURSE"),
            ("large", "TOO_LARGE"),
            ("missing", "NOT_FOUND"),
            ("denied", "FORBIDDEN"),
        ]:
            with pytest.raises(CoursesError, match=error):
                await portal.fetch(ROOT + path, ROOT, context(), limit=3)


def test_pdf_range_and_scan():
    writer = PdfWriter()
    for _ in range(12):
        writer.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()
    result = extract(data, 1, 12, 0)
    assert len(result["pages"]) == 10 and result["next_page"] == 11
    assert result["pages"][0]["has_text"] is False
    assert extract(data, 13, 13, 0)["error"].startswith("INVALID_RANGE")


@pytest.mark.asyncio
async def test_retry_after_blocks_immediate_retry():
    portal = Portal(Authentication({}))
    await portal.client.aclose()
    calls = []

    def cooldown(request):
        calls.append(request.url)
        return httpx.Response(429, headers={"Retry-After": "30"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(cooldown)) as client:
        portal.client = client
        for _ in range(2):
            with pytest.raises(CoursesError, match="RATE_LIMITED"):
                await portal.fetch(ROOT, ROOT)
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_heading_and_page_continuation():
    raw = (
        "<title>Syllabus</title><main><h1>Start</h1><h2 id='week'>Week 1</h2>"
        "<p>" + "a" * 16500 + "</p><h2>Week 2</h2><p>Stop</p></main>"
    ).encode()
    doc = Document(ROOT + "index.html", raw, "text/html", len(raw))
    site = Materials(
        SimpleNamespace(auth=Authentication({}), fetch=AsyncMock(return_value=doc))
    )
    site.home = AsyncMock(return_value=ROOT)
    first = await site.read("X", "B232", "", "week", 0, None)
    second = await site.read(
        "X", "B232", first["source"], "week", first["next_offset"], None
    )
    assert first["source"] == doc.url and first["text"].startswith("## Week 1")
    assert second["next_offset"] is None and second["text"].rstrip().endswith("a")


@pytest.mark.asyncio
async def test_course_search_finds_page_text_after_inaccessible_links(monkeypatch):
    auth = Authentication({"COURSES_OAUTH_ACCESS_TOKEN": "fixture"})
    portal = Portal(auth)
    await portal.client.aclose()
    sign_in = AsyncMock()
    monkeypatch.setattr(auth, "sign_in", sign_in)
    calls = []

    def respond(request):
        calls.append(str(request.url))
        path = request.url.path
        if path.endswith("/old.html"):
            return httpx.Response(404)
        if path.endswith("/restricted.html"):
            return httpx.Response(303, headers={"Location": IDP + "/login.html"})
        if path.endswith("/week/"):
            return httpx.Response(302, headers={"Location": "index.html"})
        if path.endswith("/week/index.html"):
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text="<main><h1>Week 1</h1><p>Počítačová grafika</p></main>",
            )
        assert str(request.url) == ROOT
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="""<header><nav><a href="other/">Other</a></nav></header>
            <div id="sidebar"><a href="old.html">Old</a>
            <a href="restricted.html">Restricted</a>
            <a href="week/">Week 1</a><a href="week/index.html">Week 1</a></div>
            <div id="content"><h1>Overview</h1><a href="https://example.org/">External</a></div>""",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        portal.client = client
        site = Materials(portal)
        site.home = AsyncMock(return_value=ROOT)
        result = await site.search_content(
            "BI-DEMO", "B232", "  pocitacova  grafika ", 0, context()
        )
        assert result["complete"] is False
        assert result["results"] == [
            {
                "title": "Week 1",
                "url": ROOT + "week/index.html",
                "snippet": "Week 1 Počítačová grafika",
            }
        ]
        sign_in.assert_not_awaited()
        assert calls.count(ROOT + "week/index.html") == 1
        assert ROOT + "other/" not in calls
        page = await site.read(
            "BI-DEMO", "B232", result["results"][0]["url"], "", 0, context()
        )
        assert "Počítačová grafika" in page["text"]
        with pytest.raises(CoursesError, match="INVALID_QUERY"):
            await site.search_content("BI-DEMO", "B232", " ", 0, context())
