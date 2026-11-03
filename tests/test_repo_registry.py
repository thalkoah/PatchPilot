"""Registration accepts only a Git checkout inside REPOSITORY_ROOT."""

from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from tests.database import open_session_factory

from patchpilot.config import get_settings
from patchpilot.db.models import TaskStatus
from patchpilot.repositories.repos import RepoRepository
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.source import __main__ as source_main
from patchpilot.source.errors import SourceError
from patchpilot.source.registry import register_checkout

SHA = "0123456789abcdef0123456789abcdef01234567"


def write_checkout(parent: Path, relative: str = "demo") -> Path:
    checkout = parent / relative
    refs = checkout / ".git" / "refs" / "heads"
    refs.mkdir(parents=True)
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (refs / "main").write_text(SHA + "\n", encoding="utf-8")
    (checkout / "app.py").write_text("print('ok')\n", encoding="utf-8")
    return checkout


@pytest.fixture
async def repos(postgres_database_url: str, clean_tasks: None):
    engine, factory = open_session_factory(postgres_database_url)
    async with factory() as session:
        yield RepoRepository(session)
    await engine.dispose()


async def test_register_stores_a_relative_path_and_is_idempotent(
    repos: RepoRepository,
    tmp_path: Path,
    postgres_database_url: str,
):
    root = tmp_path / "repos"
    root.mkdir()
    write_checkout(root, "team/demo")

    created = await register_checkout(
        repos,
        repository_root=root,
        relative_path="./team/demo",
        display_name="  Demo service  ",
    )
    again = await register_checkout(
        repos,
        repository_root=root,
        relative_path="team/demo",
        display_name="Demo service renamed",
    )

    assert again.id == created.id
    assert again.canonical_path == "team/demo"
    assert again.display_name == "Demo service renamed"
    assert again.enabled is True
    assert again.registration == {"head": SHA}
    assert str(tmp_path) not in again.canonical_path

    engine, factory = open_session_factory(postgres_database_url)
    async with factory() as session:
        tasks = TaskRepository(session)
        task = await tasks.create(
            title="Broken discount",
            description="Unknown tier returns 500.",
            repo_id=created.id,
        )
        claimed = await tasks.claim_next_queued(stage="triage")
        stored = await tasks.get(task.id)
    await engine.dispose()

    assert claimed is not None
    assert claimed.id == task.id
    assert stored is not None
    assert stored.repo_id == created.id
    assert stored.status == TaskStatus.RUNNING


@pytest.mark.parametrize(
    ("relative", "code"),
    [
        ("../outside", "invalid_path"),
        ("/tmp/outside", "invalid_path"),
        ("https://example.invalid/repo.git", "invalid_path"),
        ("demo", "not_a_checkout"),
    ],
)
async def test_register_rejects_paths_that_are_not_a_local_checkout(
    repos: RepoRepository,
    tmp_path: Path,
    relative: str,
    code: str,
):
    root = tmp_path / "repos"
    root.mkdir()
    (root / "demo").mkdir()
    outside = tmp_path / "outside"
    write_checkout(outside, "demo")

    with pytest.raises(SourceError) as caught:
        await register_checkout(
            repos,
            repository_root=root,
            relative_path=relative,
            display_name="Nope",
        )

    assert caught.value.code == code
    assert await repos.list_all() == []


async def test_register_rejects_a_symlink_checkout_that_leaves_the_root(
    repos: RepoRepository,
    tmp_path: Path,
):
    root = tmp_path / "repos"
    root.mkdir()
    outside = tmp_path / "outside"
    write_checkout(outside, "demo")
    (root / "linked").symlink_to(outside / "demo")
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir()
    checkout = write_checkout(root, "real")
    (checkout / ".git").rename(git_dir / "stolen")
    (checkout / ".git").symlink_to(git_dir / "stolen")

    with pytest.raises(SourceError) as linked:
        await register_checkout(
            repos,
            repository_root=root,
            relative_path="linked",
            display_name="Linked",
        )
    assert linked.value.code == "escapes_root"
    with pytest.raises(SourceError) as gitlink:
        await register_checkout(
            repos,
            repository_root=root,
            relative_path="real",
            display_name="Real",
        )
    assert gitlink.value.code == "not_a_checkout"
    assert await repos.list_all() == []


def test_repository_table_and_task_foreign_key(postgres_database_url: str):
    with psycopg.connect(
        postgres_database_url,
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        columns = connection.execute(
            """
            SELECT column_name, is_nullable, udt_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'repositories'
            """
        ).fetchall()
        constraints = connection.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'public.repositories'::regclass
            """
        ).fetchall()
        foreign_keys = connection.execute(
            """
            SELECT conname, pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE conrelid = 'public.tasks'::regclass AND contype = 'f'
            """
        ).fetchall()
        with pytest.raises(psycopg.Error):
            connection.execute(
                """
                INSERT INTO repositories (id, display_name, canonical_path, registration)
                VALUES (%s, %s, %s, '{}'::jsonb)
                """,
                (uuid4(), "Bad", "../etc"),
            )

    by_name = {row["column_name"]: row for row in columns}
    assert set(by_name) == {
        "id",
        "display_name",
        "canonical_path",
        "enabled",
        "registration",
        "created_at",
        "updated_at",
    }
    assert by_name["canonical_path"]["is_nullable"] == "NO"
    assert by_name["registration"]["udt_name"] == "jsonb"
    assert {row["conname"] for row in constraints} >= {
        "repositories_pkey",
        "uq_repositories_canonical_path",
        "ck_repositories_canonical_path",
        "ck_repositories_registration_object",
    }
    definitions = {row["conname"]: row["definition"] for row in foreign_keys}
    assert "fk_tasks_repo_id" in definitions
    assert "REFERENCES repositories" in definitions["fk_tasks_repo_id"]
    assert "ON DELETE RESTRICT" in definitions["fk_tasks_repo_id"]


def test_cli_registers_and_lists_without_printing_the_host_path(
    postgres_database_url: str,
    clean_tasks: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    root = tmp_path / "repos"
    root.mkdir()
    write_checkout(root, "demo_service")
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    monkeypatch.setenv("REPOSITORY_ROOT", str(root))
    get_settings.cache_clear()

    assert source_main.main(["register", "--name", "Demo service", "--path", "demo_service"]) == 0
    registered = capsys.readouterr().out.strip().split()
    assert len(registered) == 2
    UUID(registered[0])
    assert registered[1] == "demo_service"
    assert str(tmp_path) not in registered[1]

    assert source_main.main(["list"]) == 0
    listed = capsys.readouterr().out.strip()
    assert registered[0] in listed
    assert "enabled demo_service" in listed

    assert source_main.main(["register", "--name", "Demo service", "--path", "/etc"]) == 1
    assert capsys.readouterr().err.strip() == "invalid_path"
    monkeypatch.delenv("REPOSITORY_ROOT")
    get_settings.cache_clear()
    assert source_main.main(["register", "--name", "Demo service", "--path", "demo_service"]) == 2
    assert capsys.readouterr().err.strip() == "root_missing"
