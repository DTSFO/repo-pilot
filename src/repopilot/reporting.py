from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

import nh3
from markdown_it import MarkdownIt

REPORT_EXPORT_SCHEMA_VERSION = "1.0"

_MARKDOWN = MarkdownIt(
    "commonmark",
    {
        "html": False,
        "linkify": False,
        "typographer": False,
    },
).enable(["table", "strikethrough"])

_ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "code": {"class"},
}
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}
_LINK_REL = "nofollow noopener noreferrer"

_EXPORT_STYLE = """
:root{color-scheme:light dark}
body{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{max-width:880px;margin:0 auto;padding:32px 24px;line-height:1.65;overflow-wrap:anywhere}
h1,h2,h3{line-height:1.25;margin-top:1.5em}
pre{overflow:auto;padding:16px;border-radius:8px;background:#171a1f;color:#f5f5f5}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
p code,li code{padding:.15em .35em;border-radius:4px;background:#e8e8e8;color:#222}
blockquote{margin-left:0;padding-left:16px;border-left:4px solid #6b8afd;color:#667}
table{border-collapse:collapse;width:100%}
th,td{padding:8px;border:1px solid #999}
a{color:#366ad3}
""".strip()


def sanitize_markdown(rendered_html: str) -> str:
    """Sanitize a rendered Markdown HTML fragment using a strict allowlist.

    Callers rendering Markdown should normally use :func:`render_markdown`. This
    lower-level function exists so the sanitizer contract can be tested and reused.
    """

    return nh3.clean(
        rendered_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel=_LINK_REL,
        clean_content_tags={"script", "style"},
    )


def render_markdown(markdown: str) -> str:
    """Render untrusted Markdown to a sanitized HTML fragment."""

    # NUL is invalid in HTML and can create inconsistent behavior across parsers.
    source = markdown.replace("\x00", "")
    return sanitize_markdown(_MARKDOWN.render(source))


def export_html(markdown: str, *, title: str = "RepoPilot Report") -> str:
    """Build a self-contained, script-free HTML report from untrusted Markdown."""

    safe_title = escape(title, quote=True)
    report = render_markdown(markdown)
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; object-src 'none'\">"
        f"<title>{safe_title}</title><style>{_EXPORT_STYLE}</style></head>"
        f"<body><main>{report}</main></body></html>\n"
    )


def export_json(
    markdown: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    evidence: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Serialize a stable JSON report envelope with Markdown and safe HTML."""

    payload = {
        "schema_version": REPORT_EXPORT_SCHEMA_VERSION,
        "metadata": dict(metadata or {}),
        "report_markdown": markdown,
        "report_html": render_markdown(markdown),
        "evidence": [dict(item) for item in evidence],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
