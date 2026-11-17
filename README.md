# FIT Courses MCP

An independent MCP server for the FIT CTU course catalogue and teaching materials. Use it with Codex, Claude Code, OpenCode or Antigravity CLI (`agy`) to search and browse course sites. Each client runs the server locally over stdio.

## Install

From this checkout, with Python 3.11 or later on Linux or macOS, run

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/playwright install chromium
```

An existing `google-chrome` or `chromium` on PATH also works. Browser support is used for semester discovery, login and interactive renewal. Catalogue search, known-semester reads and valid cookie reads use HTTP directly. On a minimal Linux system, Playwright can install Chromium's system dependencies with `playwright install --with-deps chromium`.

## Connect a client

To try public materials without signing in, run the corresponding command from this repository. The shell expands the executable path during registration, so the MCP works when launched from another directory.

```sh
codex mcp add fit-courses -- "$PWD/.venv/bin/fit-courses-mcp"
claude mcp add --transport stdio fit-courses -- "$PWD/.venv/bin/fit-courses-mcp"
opencode mcp add fit-courses -- "$PWD/.venv/bin/fit-courses-mcp"
agy mcp add fit-courses -- "$PWD/.venv/bin/fit-courses-mcp"
```

Claude Code and OpenCode register in the current project by default. Use `claude mcp add --scope user` or `opencode mcp add fit-courses --global` with the same server command to register across projects. Open a fresh client session after registration. In OpenCode v2, use `/mcps` to check the connection if the tools are still loading.

The `agy mcp add` command registers the server for your user account. Run `agy mcp list` to verify registration and use `/mcp` inside `agy` to check the live connection. For workspace-only setup, use `.agents/mcp_config.json` with an entry under `mcpServers.fit-courses`. Set `command` to the absolute executable path and use `args` for any server arguments. [Antigravity MCP documentation](https://antigravity.google/docs/mcp#antigravity-cli)

Codex 0.169.0, Claude Code 2.1.295, OpenCode 2.3.0 and Antigravity CLI 1.5.9 each retrieved the live catalogue entry for `BI-ML1.21` in smoke tests. The OpenCode check used direct tools, enabled with `"codemode": false` in the `mcp.servers.fit-courses` entry.

## Access protected materials

Your MCP client receives page text, search snippets, and PDF extracts. Depending on your client and model settings, retrieved content may be sent to a hosted AI provider and retained in conversation history. Before reading protected materials, check whether the course rules and your provider's data settings allow that use. [CTU guidelines for AI in study and teaching](https://www.cvut.cz/sites/default/files/content/d1dc93cd-5894-4521-b799-c7e715d3c59e/en/20240220-methodical-guideline-no-52023-rev2.pdf)

Use an existing private `.env` or copy [.env.example](.env.example) into a new one. Set permissions with `chmod 600 .env`. To load the file, append `--env-file "$PWD/.env"` after the executable in your client setup command above, or add `["--env-file", "/absolute/path/to/.env"]` as the server arguments in your client configuration. Reopen the client session after updating the registration.

| Mode | What goes in the file |
|---|---|
| Cookie | Sign in to Courses in your browser. In developer tools, select cookies for `https://courses.fit.cvut.cz` and copy just the value of `oauth_access_token` into `COURSES_OAUTH_ACCESS_TOKEN`. This lets the server use your Courses session while you enter your university password only in the browser. |
| Password | Set `COURSES_USERNAME` and `COURSES_PASSWORD` to let the server sign in through the official FIT login page on the first protected read. This stores your university password as plaintext in your private local file. |

When both are present, the server tries the cookie first. When FIT rejects it, the server asks for permission to sign in again. Approval lasts for this MCP connection. It uses the saved credentials if available or opens FIT login in a separate browser window. A decline lasts until the client restarts the server. Clients with MCP form elicitation show their own consent prompt. The fallback opens a local browser confirmation window. Headless users without an elicitation-capable client can replace the cookie in the private file and restart. Renewed cookies live in process memory. Environment variables override the file. Use the local configuration file or browser for all secret values.

## Use the tools

Try **“Find BI-ML1.21 and show me its lecture pages”** or **“Find BI-3DT.1 in B232 and open its archived homepage.”**

| Tool | How to use it |
|---|---|
| `list_snapshots` | See published semester choices. `complete: false` indicates that browser-based semester discovery was unavailable. |
| `find_courses` | Search by code or Czech/English name. Use `active`, `language`, `snapshot` and `next_cursor` to filter and paginate. |
| `list_pages` | Supply an exact course code and optional title query. Results include breadcrumbs. `complete: false` indicates a bounded or interrupted crawl. |
| `search_course_content` | Search for a word or phrase inside one course's HTML pages. Results include matching snippets and source URLs. Follow `next_cursor` for more matches. |
| `read_page` | Start with the course code, then follow a returned page URL in the same session and snapshot. Supply a heading or text offset for long pages. |
| `get_attachment` | Supply the course code and a discovered attachment URL. For a PDF, request an inclusive one-based page range. Continue a partial page with both `next_page` and `next_offset`. |
| `auth_status` | Inspect configured mode, last authentication outcome and renewal permission. `untested` is normal until a protected read. |

External sites are returned as links. PDF text extraction uses a bounded worker and reports image-only pages. Course content search scans up to 100 HTML pages with a 20-second crawl budget after loading the homepage. If `complete` is false, the result covers only the pages the crawl could read. To sign in for a protected subpage, open it directly with `read_page` and retry the search.

## Development

```sh
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

The offline tests use synthetic credentials and materials.

To rerun the packaged stdio smoke test against FIT, run `FIT_COURSES_LIVE=1 .venv/bin/python -m pytest -q tests/test_live.py`. Add `FIT_COURSES_LIVE_ENV_FILE="$PWD/.env"` to test a protected read using the credentials or cookie configured there. This check uses BI-ML1.21, the B232 archive and, when enabled, BIE-BEK.21.

## License

The project code is available under the [MIT License](LICENSE). Course materials remain subject to their respective rights and terms.
