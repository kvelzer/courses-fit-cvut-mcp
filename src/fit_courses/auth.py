"""Browser sign-in and per-process renewal consent."""

import asyncio
import os
import re
import shutil
import sys
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright
from pydantic import BaseModel

ORIGIN = "https://courses.fit.cvut.cz"
IDP = "https://auth.fit.cvut.cz"
COOKIE = "oauth_access_token"


class CoursesError(Exception):
    """Only fixed, user-facing messages belong here."""


def display_available():
    return bool(
        os.getenv("DISPLAY")
        or os.getenv("WAYLAND_DISPLAY")
        or os.name == "nt"
        or sys.platform == "darwin"
    )


def chrome_options(visible=False):
    return {
        "headless": not visible,
        "executable_path": shutil.which("google-chrome") or shutil.which("chromium"),
    }


async def semesters():
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(**chrome_options())
        page = await browser.new_page()
        await page.goto(ORIGIN, wait_until="domcontentloaded")
        await page.locator('option[value^="B"]').first.wait_for(
            state="attached", timeout=10000
        )
        values = await page.locator("select option").evaluate_all(
            "opts => opts.map(o => o.value)"
        )
        return sorted({v for v in values if re.fullmatch(r"B\d{3}", v)}, reverse=True)


async def browser_login(username, password):
    visible = display_available()
    if not username and not visible:
        raise CoursesError(
            "INTERACTION_REQUIRED: Set COURSES_OAUTH_ACCESS_TOKEN locally and restart the server."
        )
    try:
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(**chrome_options(visible))
            page = await browser.new_page(locale="cs-CZ")
            await page.goto(ORIGIN, wait_until="domcontentloaded")
            await page.get_by_role(
                "button", name=re.compile(r"přihla|sign in|log in", re.IGNORECASE)
            ).first.click(timeout=15000)
            await page.wait_for_url(IDP + "/login.html*", timeout=15000)
            if username:
                form = page.locator("form").filter(
                    has=page.locator('input[name="j_password"]')
                )
                safe = await form.evaluate("""f => location.origin === 'https://auth.fit.cvut.cz'
                    && new URL(f.action).origin === location.origin && f.method === 'post'""")
                if not safe:
                    raise CoursesError("AUTH_FAILED: Unrecognized FIT login form.")
                await form.locator("[name=j_username]").fill(username)
                await form.locator("[name=j_password]").fill(password)
                await form.locator("button[type=submit]").click()
                if urlsplit(page.url).path == "/login.html" and await form.count():
                    raise CoursesError(
                        "AUTH_FAILED: FIT still shows the login form. Check your credentials."
                    )
            await page.wait_for_url(
                ORIGIN + "/**", timeout=120000 if visible else 15000
            )
            for cookie in await page.context.cookies(ORIGIN):
                if (
                    cookie["name"] == COOKIE
                    and cookie["domain"] == "courses.fit.cvut.cz"
                ):
                    return cookie["value"]
    except BrowserError:
        raise CoursesError(
            "AUTH_FAILED: FIT sign-in did not finish. Check browser installation, credentials or MFA, or supply a fresh cookie."
        ) from None
    raise CoursesError("AUTH_FAILED: FIT did not issue a Courses access cookie.")


class Renew(BaseModel):
    approve: bool


async def ask_renewal(ctx):
    message = "May FIT Courses sign in again to renew your cookie for this session?"
    capabilities = ctx.session.client_params.capabilities
    elicitation = getattr(capabilities, "elicitation", None)
    if elicitation is not None and (
        getattr(elicitation, "form", None) is not None
        or getattr(elicitation, "url", None) is None
    ):
        result = await ctx.elicit(message, Renew)
        return result.action == "accept" and result.data.approve
    if display_available():
        try:
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(**chrome_options(visible=True))
                page = await browser.new_page()
                await page.set_content(f"""<title>FIT Courses sign-in</title><h1>Renew your Courses cookie?</h1>
                    <p>{message}</p><button onclick="window.choice='yes'">Allow for this session</button>
                    <button onclick="window.choice='no'">Decline</button>""")
                decision = await page.wait_for_function("window.choice", timeout=120000)
                return await decision.json_value() == "yes"
        except BrowserError:
            return False
    raise CoursesError(
        "INTERACTION_REQUIRED: Replace COURSES_OAUTH_ACCESS_TOKEN in your private configuration and restart."
    )


class Authentication:
    def __init__(self, config):
        self.username = config.get("COURSES_USERNAME")
        self.password = config.get("COURSES_PASSWORD")
        self.token = config.get("COURSES_OAUTH_ACCESS_TOKEN")
        if bool(self.username) != bool(self.password):
            raise CoursesError(
                "Set both COURSES_USERNAME and COURSES_PASSWORD or leave both blank."
            )
        if self.token and (
            not re.fullmatch(r"[!-~]+", self.token) or ";" in self.token
        ):
            raise CoursesError("Invalid access cookie format. Supply only its value.")
        self.source = (
            "cookie" if self.token else "password" if self.username else "anonymous"
        )
        self.outcome = "untested"
        self.approval = "unasked"
        self.lock = asyncio.Lock()
        self.generation = 0

    async def sign_in(self, ctx, generation):
        async with self.lock:
            if generation != self.generation:
                return
            if self.outcome == "login_failed":
                raise CoursesError(
                    "AUTH_FAILED: Update the configuration and restart to retry sign-in."
                )
            if self.token or self.generation:
                self.outcome = "cookie_rejected"
                if self.approval == "unasked":
                    self.approval = "granted" if await ask_renewal(ctx) else "declined"
                if self.approval != "granted":
                    raise CoursesError(
                        "RENEWAL_DECLINED: Supply a fresh cookie locally and restart."
                    )
            elif not self.username:
                raise CoursesError(
                    "AUTH_REQUIRED: Configure a Courses cookie or FIT credentials."
                )
            try:
                self.token = await browser_login(self.username, self.password)
            except CoursesError:
                self.outcome = "login_failed"
                raise
            self.outcome = "signed_in"
            self.generation += 1
