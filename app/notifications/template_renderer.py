"""Jinja2 email template renderer."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_template(template_name: str, context: dict) -> tuple[str, str]:
    """Render an email template and return ``(html, plaintext_fallback)``."""
    html_template = _env.get_template(template_name)
    html = html_template.render(**context)

    # Try to load a matching .txt template for plaintext; fall back to stripping tags
    txt_name = template_name.replace(".html", ".txt")
    try:
        txt_template = _env.get_template(txt_name)
        plaintext = txt_template.render(**context)
    except Exception:
        # Simple tag-strip fallback
        import re
        plaintext = re.sub(r"<[^>]+>", "", html)
        plaintext = re.sub(r"\n{3,}", "\n\n", plaintext).strip()

    return html, plaintext
