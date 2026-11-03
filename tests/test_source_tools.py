"""Read-only repository tools stay inside one checkout."""

from pathlib import Path

import pytest

from patchpilot.source.errors import SourceError
from patchpilot.source.limits import HARD_MAX_FILE_BYTES, ToolLimits
from patchpilot.source.tools import RepositoryTools, open_repository_tools

ROOT = Path(__file__).resolve().parents[1]


def write_checkout(parent: Path, relative: str = "demo") -> Path:
    checkout = parent / relative
    git_dir = checkout / ".git" / "refs" / "heads"
    git_dir.mkdir(parents=True)
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "main").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
    (checkout / "app.py").write_text("print('ok')\n", encoding="utf-8")
    return checkout


def test_limits_cannot_exceed_the_hard_ceiling():
    with pytest.raises(ValueError):
        ToolLimits(max_file_bytes=0)
    with pytest.raises(ValueError):
        ToolLimits(max_file_bytes=HARD_MAX_FILE_BYTES + 1)


def test_tools_read_and_search_the_checkout(tmp_path: Path):
    checkout = write_checkout(tmp_path)
    (checkout / "notes.md").write_text("discount returns 500\n", encoding="utf-8")
    tools = RepositoryTools(checkout)

    listing = tools.list_tree(".")
    paths = {entry.path: entry.kind for entry in listing.entries}

    assert paths["app.py"] == "file"
    assert paths["notes.md"] == "file"
    assert ".git" not in paths
    assert all(not entry.path.startswith("/") for entry in listing.entries)
    text = tools.read_text_file("app.py")
    assert text.content == "print('ok')\n"
    assert text.path == "app.py"
    assert text.truncated is False
    found = tools.search_text("500", ".")
    assert [(match.path, match.line) for match in found.matches] == [("notes.md", 1)]
    assert found.truncated is False


def test_internal_symlink_reads_the_target_file(tmp_path: Path):
    checkout = write_checkout(tmp_path)
    (checkout / "alias.py").symlink_to(checkout / "app.py")
    tools = RepositoryTools(checkout)

    text = tools.read_text_file("alias.py")

    assert text.path == "app.py"
    assert text.content == "print('ok')\n"
    assert str(tmp_path) not in text.path


def test_rejected_paths_do_not_escape_or_reveal_secrets(tmp_path: Path):
    root = tmp_path / "repos"
    root.mkdir()
    checkout = write_checkout(root)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.py").write_text("OUTSIDE_NOTE\n", encoding="utf-8")
    (checkout / "leak.py").symlink_to(secret)
    (checkout / "linked").symlink_to(outside)
    (checkout / ".git" / "config").write_text("password = hunter2\n", encoding="utf-8")
    (checkout / ".env").write_text(
        "DATABASE_URL=postgresql://user:hunter2@db/db\n",
        encoding="utf-8",
    )
    (checkout / "id_rsa").write_text("private-key\n", encoding="utf-8")
    (checkout / "secrets.json").write_text('{"token":"hunter2"}\n', encoding="utf-8")
    (checkout / "token.pem").write_text("-----BEGIN\n", encoding="utf-8")
    tools = RepositoryTools(checkout)

    attacks = [
        "../secret.txt",
        "../../secret.txt",
        str(secret),
        "/etc/passwd",
        "..\\secret.txt",
        ".git/config",
        ".env",
        ".env.local",
        "id_rsa",
        "secrets.json",
        "token.pem",
        "leak.py",
        "linked/note.py",
    ]
    for relative in attacks:
        with pytest.raises(SourceError) as caught:
            tools.read_text_file(relative)
        message = str(caught.value)
        assert "TOPSECRET" not in message
        assert "hunter2" not in message
        assert str(tmp_path) not in message
        assert str(secret) not in message

    listing = tools.list_tree(".")
    rendered = "\n".join(entry.path for entry in listing.entries)
    assert ".git" not in rendered
    assert ".env" not in rendered
    assert "id_rsa" not in rendered
    assert "secrets.json" not in rendered
    assert "token.pem" not in rendered
    assert "linked" in rendered
    assert "note.py" not in rendered
    assert str(tmp_path) not in rendered
    found = tools.search_text("hunter2")
    assert found.matches == []
    outside_hit = tools.search_text("OUTSIDE_NOTE")
    assert outside_hit.matches == []
    secret_hit = tools.search_text("TOPSECRET")
    assert secret_hit.matches == []


def test_binary_and_oversized_files_are_refused(tmp_path: Path):
    checkout = write_checkout(tmp_path)
    (checkout / "big.py").write_text("x" * 40, encoding="utf-8")
    (checkout / "bin.py").write_bytes(b"print(1)\n\x00\xff")
    (checkout / "lines.py").write_text(
        "\n".join(str(index) for index in range(6)),
        encoding="utf-8",
    )
    tools = RepositoryTools(
        checkout,
        limits=ToolLimits(max_file_bytes=32, max_file_lines=4),
    )

    with pytest.raises(SourceError) as oversized:
        tools.read_text_file("big.py")
    assert oversized.value.code == "too_large"
    with pytest.raises(SourceError) as binary:
        tools.read_text_file("bin.py")
    assert binary.value.code == "binary"
    text = tools.read_text_file("lines.py")
    assert text.truncated is True
    assert text.line_count == 4
    assert text.content.splitlines() == ["0", "1", "2", "3"]
    skipped = tools.search_text("x")
    assert all(match.path != "big.py" for match in skipped.matches)


def test_tree_and_search_stop_at_their_counts(tmp_path: Path):
    checkout = write_checkout(tmp_path)
    for index in range(4):
        (checkout / f"extra_{index}.py").write_text(f"marker {index}\n", encoding="utf-8")
    tools = RepositoryTools(
        checkout,
        limits=ToolLimits(max_tree_entries=3, max_search_files=2, max_search_matches=1),
    )

    listing = tools.list_tree(".")
    assert listing.truncated is True
    assert len(listing.entries) == 3
    found = tools.search_text("marker")
    assert found.truncated is True
    assert len(found.matches) == 1


def test_unregistered_and_absolute_paths_cannot_select_a_checkout(tmp_path: Path):
    root = tmp_path / "repos"
    root.mkdir()
    write_checkout(root, "demo")
    outside = tmp_path / "other"
    write_checkout(outside, "demo")
    (root / "linked").symlink_to(outside / "demo")

    with pytest.raises(SourceError) as parent:
        open_repository_tools(root, "../other/demo")
    assert parent.value.code == "invalid_path"
    with pytest.raises(SourceError) as absolute:
        open_repository_tools(root, str(outside / "demo"))
    assert absolute.value.code == "invalid_path"
    with pytest.raises(SourceError) as linked:
        open_repository_tools(root, "linked")
    assert linked.value.code == "escapes_root"
    with pytest.raises(SourceError) as missing:
        open_repository_tools(root, "missing")
    assert missing.value.code == "not_found"

    tools = open_repository_tools(root, "demo")
    assert tools.read_text_file("app.py").content == "print('ok')\n"
    with pytest.raises(SourceError):
        tools.read_text_file("/etc/passwd")


def test_demo_service_is_readable_and_the_unknown_tier_still_raises():
    import importlib.util

    tools = open_repository_tools(ROOT / "examples", "demo_service")
    listing = tools.list_tree(".")
    paths = {entry.path for entry in listing.entries}

    assert "src/demo_service/discounts.py" in paths
    assert all(".git" not in entry.path.split("/") for entry in listing.entries)
    text = tools.read_text_file("src/demo_service/discounts.py")
    assert "KNOWN_TIERS" in text.content
    assert str(ROOT) not in text.content
    assert text.path == "src/demo_service/discounts.py"
    found = tools.search_text("platinum")
    assert any(match.path == "tests/test_unknown_tier.py" for match in found.matches)

    discounts = ROOT / "examples" / "demo_service" / "src" / "demo_service" / "discounts.py"
    spec = importlib.util.spec_from_file_location("demo_discounts_under_test", discounts)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.discount_percent("gold") == 20
    with pytest.raises(KeyError):
        module.discount_percent("platinum")
