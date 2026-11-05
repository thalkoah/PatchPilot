"""Owner and repository names. A model string is never accepted here."""

import re
from dataclasses import dataclass

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass(frozen=True, slots=True)
class GitHubRepo:
    """One allowlisted GitHub repository. This is not a remote URL."""

    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    def git_remote(self) -> str:
        """The only HTTPS remote a push may use. It contains no user or token."""
        return f"https://github.com/{self.owner}/{self.name}.git"

    def pull_url(self, number: int) -> str:
        return f"https://github.com/{self.owner}/{self.name}/pull/{number}"


def parse_slug(value: str) -> tuple[str, str]:
    """Return `owner, name`. Reject URLs, extra segments, and blank text."""
    if not isinstance(value, str):
        raise ValueError("github repository must be owner/name")
    slug = value.strip()
    owner, separator, name = slug.partition("/")
    if (
        separator != "/"
        or owner == ""
        or name == ""
        or "/" in name
        or _NAME.fullmatch(owner) is None
        or _NAME.fullmatch(name) is None
    ):
        raise ValueError("github repository must be owner/name")
    return owner, name


def parse_allowed_repos(value: str) -> frozenset[str]:
    """Parse a comma-separated allowlist. An empty string allows nothing."""
    if not isinstance(value, str):
        raise ValueError("github_allowed_repos must be owner/name pairs")
    text = value.strip()
    if text == "":
        return frozenset()
    slugs: list[str] = []
    for part in text.split(","):
        owner, name = parse_slug(part)
        slugs.append(f"{owner}/{name}")
    return frozenset(slugs)


def repo_from_registration(registration: object, *, allowed: frozenset[str]) -> GitHubRepo | None:
    """Return the registered repository when it is on the allowlist.

    `None` means this checkout is not a publication target. The coordinates
    come from the registry row, not from a task or a model.
    """
    if not isinstance(registration, dict):
        return None
    github = registration.get("github")
    if not isinstance(github, dict):
        return None
    owner = github.get("owner")
    name = github.get("name")
    if not isinstance(owner, str) or not isinstance(name, str):
        return None
    slug = f"{owner}/{name}"
    if slug not in allowed:
        return None
    try:
        checked_owner, checked_name = parse_slug(slug)
    except ValueError:
        return None
    return GitHubRepo(owner=checked_owner, name=checked_name)
