"""The desk's pages: Jinja2 templates with autoescaping on, so whatever an agent wrote (its words, the call's
names and arguments) reaches the page only as escaped text, and only in element content, never in an attribute,
a URL or a script. Control, format and separator characters are shown as visible escapes (a right-to-left override
or a zero-width space would otherwise change what a person reads without showing up)."""

import unicodedata
from importlib.resources import files
from typing import Final

from jinja2 import Environment, PackageLoader

from tool_approvals.approvals.app.answers import ApprovalDetail

_PACKAGE: Final = "tool_approvals.approvals.edges.desk"

STYLESHEET: Final = files(_PACKAGE).joinpath("desk.css").read_bytes()
_HIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})
_KEPT: Final = frozenset("\n")
_TITLE: Final = "Review a tool call"


def visible(text: str) -> str:
    """`text` with every control, format and line or paragraph separator character but the line feed written as
    a visible escape (`\\u202e`)."""
    return "".join(_shown(char) for char in text)


def _shown(char: str) -> str:
    if char in _KEPT or unicodedata.category(char) not in _HIDDEN_CATEGORIES:
        return char
    point = ord(char)
    return f"\\u{point:04x}" if point <= 0xFFFF else f"\\U{point:08x}"


_ENVIRONMENT: Final = Environment(loader=PackageLoader(_PACKAGE), autoescape=True, auto_reload=False)
_ENVIRONMENT.filters["visible"] = visible
_REVIEW: Final = _ENVIRONMENT.get_template("review.html")
_MESSAGE: Final = _ENVIRONMENT.get_template("message.html")


def review_page(detail: ApprovalDetail, *, stylesheet: str, action: str, digest: str, form_token: str | None) -> str:
    """The review of one Approval; with the answer form when `form_token` is given."""
    return _REVIEW.render(
        title=_TITLE,
        stylesheet=stylesheet,
        detail=detail,
        arguments=detail.call.arguments_json.decode(errors="replace"),
        action=action,
        digest=digest,
        form_token=form_token,
    )


def message_page(title: str, message: str, *, stylesheet: str) -> str:
    return _MESSAGE.render(title=title, message=message, stylesheet=stylesheet)
