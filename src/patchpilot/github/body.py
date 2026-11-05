"""Draft pull request text. The token is not an argument."""

from urllib.parse import quote

DISCLOSURE = (
    "This pull request was drafted by PatchPilot. "
    "The description and the patch were produced by software. "
    "It is a draft and has not been merged."
)
_TITLE_LIMIT = 200
_BODY_LIMIT = 8000
_TEXT_LIMIT = 1500


def pull_request_title(title: str) -> str:
    """One line. The task title is clipped and the disclosure stays in the body."""
    text = _line(title, _TITLE_LIMIT - len("PatchPilot draft: "))
    if text == "":
        text = "maintenance change"
    return f"PatchPilot draft: {text}"


def pull_request_body(
    *,
    description: str,
    reasoning: str,
    owner: str,
    name: str,
    branch: str,
    base: str,
    tests_passed: bool,
    tests_summary: str,
) -> str:
    """Issue summary, reasoning, a compare link, the test result, and the disclosure."""
    compare = (
        f"https://github.com/{owner}/{name}/compare/"
        f"{quote(base, safe='')}...{quote(branch, safe='')}"
    )
    tests = "passed" if tests_passed else "did not pass"
    parts = [
        "## Issue",
        _line(description, _TEXT_LIMIT),
        "## Reasoning",
        _line(reasoning, _TEXT_LIMIT),
        "## Diff",
        compare,
        "## Tests",
        f"The restricted pytest run {tests}. {_line(tests_summary, 300)}",
        "## Disclosure",
        DISCLOSURE,
    ]
    body = "\n\n".join(part for part in parts if part)
    if DISCLOSURE not in body:
        body = DISCLOSURE
    return body[:_BODY_LIMIT]


def _line(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:limit]
