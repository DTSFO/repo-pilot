from __future__ import annotations

import json

import pytest

from repopilot.reporting import export_html, export_json, render_markdown, sanitize_markdown


def test_render_markdown_preserves_report_structure() -> None:
    rendered = render_markdown(
        "# Report\n\n**finding** and `code`\n\n"
        "| source | score |\n| --- | --- |\n| `api.py:L1-L2` | 1.0 |\n\n"
        "```python\nprint('<safe>')\n```"
    )

    assert "<h1>Report</h1>" in rendered
    assert "<strong>finding</strong>" in rendered
    assert "<table>" in rendered
    assert '<code class="language-python">' in rendered
    assert "&lt;safe&gt;" in rendered


@pytest.mark.parametrize(
    "attack",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "<svg><script>alert(1)</script></svg>",
        "<math><mtext><img src=x onerror=alert(1)></mtext></math>",
        '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
        '<a href="javascript:alert(1)" onclick="alert(2)">click</a>',
        '<style>@import "https://attacker.invalid/x.css"</style>',
        '<form action="https://attacker.invalid"><input name=x></form>',
    ],
)
def test_raw_html_attacks_are_rendered_as_inert_text(attack: str) -> None:
    rendered = render_markdown(attack)

    assert "<script" not in rendered
    assert "<img" not in rendered
    assert "<svg" not in rendered
    assert "<math" not in rendered
    assert "<iframe" not in rendered
    assert "<style" not in rendered
    assert "<form" not in rendered
    # Raw HTML is escaped as visible text. Attribute names may remain visible,
    # but there is no browser element on which they could execute.
    assert "&lt;" in rendered


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java&#x73;cript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
    ],
)
def test_unsafe_markdown_link_protocols_are_not_clickable(url: str) -> None:
    rendered = render_markdown(f"[click]({url})")

    assert "href=" not in rendered
    assert "<a " not in rendered


@pytest.mark.parametrize("url", ["https://example.com/a", "http://example.com", "mailto:a@b.test"])
def test_allowed_links_receive_safe_rel(url: str) -> None:
    rendered = render_markdown(f"[source]({url})")

    assert f'href="{url}"' in rendered
    assert 'rel="nofollow noopener noreferrer"' in rendered
    assert "target=" not in rendered


def test_sanitizer_removes_dangerous_html_even_when_called_directly() -> None:
    sanitized = sanitize_markdown(
        '<p class="unsafe">ok <a href="javascript:alert(1)" onclick="alert(2)">x</a></p>'
        "<script>alert(3)</script>"
    )

    assert sanitized == '<p>ok <a rel="nofollow noopener noreferrer">x</a></p>'


def test_code_fence_keeps_attack_text_inert_and_visible() -> None:
    rendered = render_markdown("```html\n<script>alert(1)</script>\n```")

    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered


def test_export_html_is_self_contained_and_escapes_title() -> None:
    exported = export_html(
        "# Result\n\n<script>alert(1)</script>", title="A </title><script>x</script>"
    )

    assert exported.startswith("<!doctype html>")
    assert "Content-Security-Policy" in exported
    assert "default-src 'none'" in exported
    assert "<main><h1>Result</h1>" in exported
    assert "<script>" not in exported
    assert "&lt;/title&gt;&lt;script&gt;x&lt;/script&gt;" in exported
    assert "https://" not in exported
    assert "http://" not in exported


def test_export_json_has_stable_utf8_envelope_and_safe_html() -> None:
    exported = export_json(
        "# 结论\n\n[bad](javascript:alert(1))",
        metadata={"task_id": "task-1", "repository_revision": "abc123"},
        evidence=[{"citation": "src/a.py:L1-L2", "accepted": True}],
    )
    payload = json.loads(exported)

    assert exported.endswith("\n")
    assert "结论" in exported
    assert payload == {
        "schema_version": "1.0",
        "metadata": {"repository_revision": "abc123", "task_id": "task-1"},
        "report_markdown": "# 结论\n\n[bad](javascript:alert(1))",
        "report_html": "<h1>结论</h1>\n<p>[bad](javascript:alert(1))</p>\n",
        "evidence": [{"accepted": True, "citation": "src/a.py:L1-L2"}],
    }
