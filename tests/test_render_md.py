"""render_md must not pass raw HTML through — captured prompt/response text is
attacker-influenced (you paste arbitrary text into Claude) and is rendered into
turn.html / diff.html with `|md|safe`. Raw <script>/<img onerror> would be a
stored XSS. Markdown formatting (bold, code, links) must still work.
"""
from __future__ import annotations


def test_render_md_escapes_raw_html_script():
    from telemetrify.ui.app import render_md
    out = render_md("hello <script>alert(1)</script> world")
    assert "<script>" not in out
    assert "alert(1)" in out  # shown as text, escaped — not executed


def test_render_md_escapes_img_onerror():
    from telemetrify.ui.app import render_md
    out = render_md('<img src=x onerror="alert(1)">')
    assert "<img" not in out.lower()


def test_render_md_still_renders_markdown():
    from telemetrify.ui.app import render_md
    out = render_md("**bold** and `code`")
    assert "<strong>bold</strong>" in out
    assert "<code>code</code>" in out


def test_render_md_blocks_javascript_url():
    from telemetrify.ui.app import render_md
    # markdown-it's validateLink rejects the dangerous protocol, so NO anchor
    # is produced — the URL is left as inert text, never a clickable
    # href="javascript:..." link.
    out = render_md("[click](javascript:alert(1))")
    assert "<a " not in out
    assert 'href="javascript:' not in out
