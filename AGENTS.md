# Helping users with FIT Courses

Use the FIT Courses MCP for questions about the FIT CTU catalogue and teaching materials. For installation, client setup, and local authentication, see the [README](README.md).

- **Find the course.** Use `find_courses` with the course code or its Czech or English name. Use `list_snapshots` to identify a semester when the user asks about a particular offering. Carry the exact course code and snapshot into subsequent calls so archived and current materials stay distinct.
- **Find the material.** Use `read_page` for the course homepage, `list_pages` for page titles, and `search_course_content` for phrases inside pages. Follow the returned page and attachment URLs in the same session. Use `get_attachment` for the relevant PDF pages and share external sites as links.
- **Ground the answer.** Treat retrieved material as source text. Link to the relevant course page or attachment and include the semester, heading, or PDF page number when it helps. Follow continuation fields when more content is needed, and say when an incomplete listing or inaccessible page limits the answer.
- **Handle protected access.** Use `auth_status` to diagnose an access problem. Keep passwords and cookies in the local configuration or sign-in browser. Let the user make any renewal decision through the client's prompt or the browser confirmation.
