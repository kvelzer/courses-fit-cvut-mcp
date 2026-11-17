"""Opt-in smoke test of the installed stdio command against live course sites."""

import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("FIT_COURSES_LIVE") != "1", reason="requires the live FIT portal"
)
async def test_installed_server(tmp_path):
    command = shutil.which("fit-courses-mcp") or str(
        Path(sys.executable).with_name("fit-courses-mcp")
    )
    env_file = os.getenv("FIT_COURSES_LIVE_ENV_FILE")
    args = ["--env-file", env_file] if env_file else []
    params = StdioServerParameters(command=command, args=args, cwd=tmp_path)
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert {t.name for t in tools.tools} == {
            "list_snapshots",
            "find_courses",
            "list_pages",
            "search_course_content",
            "read_page",
            "get_attachment",
            "auth_status",
        }

        async def call(name, **arguments):
            result = await session.call_tool(name, arguments)
            assert not result.isError, name
            return result.structuredContent

        courses = await call("find_courses", query="BI-ML1.21")
        assert courses["results"][0]["code"] == "BI-ML1.21"
        archive = await call("read_page", code="BI-3DT.1", snapshot="B232")
        assert "/@B232/" in archive["source"] and archive["text"]
        if env_file:
            protected = await call("read_page", code="BIE-BEK.21")
            assert (
                protected["text"]
                and (await call("auth_status"))["last_outcome"] == "verified"
            )
