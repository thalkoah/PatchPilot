"""Draft publication stays behind a second approval and a mocked GitHub API."""

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from tests.database import open_session_factory
from tests.runner_support import start_runner, stop_runner
from tests.test_approvals import DEMO_DESCRIPTION, DEMO_TITLE, _decide
from tests.test_workspace import copy_demo_repository, git_snapshot
from tests.workflow_support import compiled_worker_graph

from patchpilot.api import create_app
from patchpilot.config import get_settings
from patchpilot.github.body import DISCLOSURE
from patchpilot.github.client import GitHubClient
from patchpilot.github.errors import GitHubError
from patchpilot.github.names import GitHubRepo, repo_from_registration
from patchpilot.github.publish import GitHubOptions, publish_draft
from patchpilot.github.push import push_task_branch
from patchpilot.llm.fake import FakeProvider
from patchpilot.repositories.repos import RepoRepository
from patchpilot.worker import process_one

TOKEN = "gho_test_token_not_for_prompts"
OWNER = "octocat"
NAME = "demo-service"
REPO = GitHubRepo(owner=OWNER, name=NAME)


@pytest.fixture
def artifacts_root(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir()
    return root


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture
def client(
    postgres_database_url: str,
    clean_tasks: None,
    artifacts_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DATABASE_URL", postgres_database_url)
    monkeypatch.setenv("ARTIFACTS_ROOT", str(artifacts_root))
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


def test_an_unlisted_repository_is_not_a_publication_target():
    registration = {"head": "a" * 40, "github": {"owner": OWNER, "name": NAME}}

    assert repo_from_registration(registration, allowed=frozenset()) is None
    assert repo_from_registration({"head": "a" * 40}, allowed=frozenset({REPO.slug})) is None
    assert repo_from_registration(registration, allowed=frozenset({REPO.slug})) == REPO


def test_a_missing_token_makes_no_request(tmp_path: Path):
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        raise AssertionError(request.url.path)

    with pytest.raises(GitHubError) as caught:
        publish_draft(
            task_id=uuid4(),
            worktree=tmp_path,
            repo=REPO,
            token=None,
            issue_title="Unused",
            description="Unused",
            reasoning="Unused",
            tests_passed=True,
            tests_summary="passed",
            transport=httpx2.MockTransport(handler),
        )

    assert caught.value.code == "token_missing"
    assert calls == []


def test_push_refuses_a_branch_that_is_not_the_task_branch(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "README").write_text("demo\n", encoding="utf-8")
    _git(source, "add", "README")
    _git(
        source,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-m",
        "init",
    )
    bare = tmp_path / "remote.git"
    _git(bare, "init", "--bare")

    with pytest.raises(GitHubError) as caught:
        push_task_branch(
            source,
            task_id=uuid4(),
            repo=REPO,
            token=SecretStr(TOKEN),
            local_remote=bare,
        )

    assert caught.value.code == "branch_refused"
    assert _refs(bare) == []
    assert TOKEN not in _config(source)


def test_github_rejects_an_unauthorized_token_without_opening_a_pull_request():
    transport, calls = _scripted(status=401)

    client = GitHubClient(SecretStr(TOKEN), transport=transport)
    try:
        with pytest.raises(GitHubError) as caught:
            client.open_draft(
                REPO,
                branch=f"patchpilot/{uuid4()}",
                title="PatchPilot draft: unused",
                body=DISCLOSURE,
                head_sha="a" * 40,
            )
    finally:
        client.close()

    assert caught.value.code == "unauthorized"
    assert calls
    assert all("merge" not in path for _method, path in calls)
    assert TOKEN not in json.dumps(calls)


def test_a_network_error_is_not_stored_as_a_pull_request():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    client = GitHubClient(SecretStr(TOKEN), transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(GitHubError) as caught:
            client.default_branch(REPO)
    finally:
        client.close()

    assert caught.value.code == "network"


def test_an_existing_draft_is_reused_instead_of_opening_another():
    task_id = uuid4()
    branch = f"patchpilot/{task_id}"
    sha = "b" * 40
    transport, calls = _draft_api(branch=branch, sha=sha)
    client = GitHubClient(SecretStr(TOKEN), transport=transport)
    try:
        first = client.open_draft(
            REPO,
            branch=branch,
            title="PatchPilot draft: discount",
            body=f"Details\n\n{DISCLOSURE}",
            head_sha=sha,
        )
        second = client.open_draft(
            REPO,
            branch=branch,
            title="PatchPilot draft: discount",
            body=f"Details\n\n{DISCLOSURE}",
            head_sha=sha,
        )
    finally:
        client.close()

    assert first == second
    assert first.draft is True
    assert first.url == f"https://github.com/{OWNER}/{NAME}/pull/7"
    assert first.branch == branch
    assert first.base == "main"
    posts = [path for method, path in calls if method == "POST"]
    assert posts == [f"/repos/{OWNER}/{NAME}/pulls"]
    assert all("merge" not in path for _method, path in calls)


async def test_rejecting_publication_sends_nothing(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
    workspace_root: Path,
    tmp_path: Path,
):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, request.url.path))
        raise AssertionError(request.url.path)

    bare = tmp_path / "remote.git"
    _git(bare, "init", "--bare")
    task_id, _before = await _pause_for_publication(
        client,
        postgres_database_url,
        artifacts_root,
        workspace_root,
        tmp_path,
        transport=httpx2.MockTransport(handler),
        local_remote=bare,
    )
    approvals = client.get(f"/api/v1/tasks/{task_id}/approvals").json()["items"]
    pending = next(item for item in approvals if item["status"] == "pending")
    assert pending["kind"] == "publish"
    page = client.get(f"/tasks/{task_id}")
    assert "Nothing has been pushed" in page.text
    assert TOKEN not in page.text

    _decide(client, task_id, pending["id"], "reject", "Do not publish.")
    repeated = _decide(client, task_id, pending["id"], "reject", "Again")
    assert repeated["status"] == "rejected"
    assert repeated["reason"] == "Do not publish."
    await _finish_publication(
        client,
        postgres_database_url,
        artifacts_root,
        workspace_root,
        tmp_path,
        transport=httpx2.MockTransport(handler),
        local_remote=bare,
    )

    done = client.get(f"/api/v1/tasks/{task_id}")
    assert done.status_code == 200
    assert done.json()["status"] == "completed"
    assert done.json()["result_json"]["publication"]["decision"] == "reject"
    assert "url" not in done.json()["result_json"]["publication"]
    assert client.get(f"/api/v1/tasks/{task_id}/publication").status_code == 404
    assert calls == []
    assert _refs(bare) == []
    assert TOKEN not in done.text


async def test_approval_opens_one_draft_and_a_retry_does_not_open_another(
    client: TestClient,
    postgres_database_url: str,
    artifacts_root: Path,
    workspace_root: Path,
    tmp_path: Path,
):
    transport, calls = _draft_api(workspace_root=workspace_root)
    bare = tmp_path / "remote.git"
    _git(bare, "init", "--bare")
    task_id, before = await _pause_for_publication(
        client,
        postgres_database_url,
        artifacts_root,
        workspace_root,
        tmp_path,
        transport=transport,
        local_remote=bare,
    )
    if before is None:
        before = git_snapshot(tmp_path / "repos" / "demo_service")
    approvals = client.get(f"/api/v1/tasks/{task_id}/approvals").json()["items"]
    pending = next(item for item in approvals if item["status"] == "pending")
    assert calls == []
    assert _refs(bare) == []

    first = _decide(client, task_id, pending["id"], "approve", "Publish the draft.")
    second = _decide(client, task_id, pending["id"], "approve", "Again")
    assert second["id"] == first["id"]
    assert second["reason"] == "Publish the draft."
    conflict = client.post(
        f"/api/v1/tasks/{task_id}/decisions",
        json={"approval_id": pending["id"], "decision": "reject", "reason": "No."},
    )
    assert conflict.status_code == 409
    await _finish_publication(
        client,
        postgres_database_url,
        artifacts_root,
        workspace_root,
        tmp_path,
        transport=transport,
        local_remote=bare,
    )

    done = client.get(f"/api/v1/tasks/{task_id}")
    body = done.json()
    publication = body["result_json"]["publication"]
    assert body["status"] == "completed"
    assert publication["draft"] is True
    assert publication["number"] == 7
    assert publication["branch"] == f"patchpilot/{task_id}"
    assert publication["base"] == "main"
    assert publication["url"] == f"https://github.com/{OWNER}/{NAME}/pull/7"
    assert TOKEN not in done.text
    listed = client.get(f"/api/v1/tasks/{task_id}/publication")
    assert listed.status_code == 200
    assert listed.json()["html_url"] == publication["url"]
    assert listed.json()["draft"] is True
    posts = [path for method, path in calls if method == "POST"]
    assert posts == [f"/repos/{OWNER}/{NAME}/pulls"]
    assert all("merge" not in path for _method, path in calls)
    assert _refs(bare) == [f"refs/heads/patchpilot/{task_id}"]
    assert "refs/heads/main" not in _refs(bare)
    assert git_snapshot(tmp_path / "repos" / "demo_service") == before
    page = client.get(f"/tasks/{task_id}")
    assert f"https://github.com/{OWNER}/{NAME}/pull/7" in page.text
    assert "was not merged" in page.text
    assert TOKEN not in page.text
    events = client.get(f"/api/v1/tasks/{task_id}/events").json()["items"]
    assert [item["event_type"] for item in events].count("publication_opened") == 1


def _draft_api(
    *,
    branch: str | None = None,
    sha: str | None = None,
    workspace_root: Path | None = None,
) -> tuple[httpx2.BaseTransport, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []
    created: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, request.url.path))
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if "merge" in request.url.path.split("/"):
            raise AssertionError(request.url.path)
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            return httpx2.Response(200, json=created)
        if request.method == "GET":
            return httpx2.Response(200, json={"default_branch": "main"})
        payload = json.loads(request.content)
        assert payload["draft"] is True
        assert payload["base"] == "main"
        assert payload["head"] != "main"
        assert str(payload["head"]).startswith("patchpilot/")
        assert TOKEN not in payload["body"]
        assert DISCLOSURE in payload["body"]
        head = str(payload["head"])
        if branch is not None:
            assert head == branch
        commit = sha if sha is not None else _worktree_head(workspace_root, head)
        opened = {
            "number": 7,
            "html_url": f"https://github.com/{OWNER}/{NAME}/pull/7",
            "draft": True,
            "head": {"ref": head, "sha": commit},
            "base": {"ref": "main"},
        }
        created.append(opened)
        return httpx2.Response(201, json=opened)

    return httpx2.MockTransport(handler), calls


def _scripted(*, status: int) -> tuple[httpx2.BaseTransport, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, request.url.path))
        return httpx2.Response(status, json={"message": "no"})

    return httpx2.MockTransport(handler), calls


async def _pause_for_publication(
    client: TestClient,
    database_url: str,
    artifacts_root: Path,
    workspace_root: Path,
    tmp_path: Path,
    *,
    transport: httpx2.BaseTransport,
    local_remote: Path,
) -> tuple[str, dict[str, str]]:
    repo_root = tmp_path / "repos"
    if not (repo_root / "demo_service").exists():
        copy_demo_repository(repo_root)
    before = git_snapshot(repo_root / "demo_service")
    provider = FakeProvider()
    engine, factory = open_session_factory(database_url)
    options = _options(transport, local_remote)
    runner = None
    runner_dir = tmp_path / "runner"
    try:
        async with factory() as session:
            repository = await RepoRepository(session).save(
                display_name="Demo service",
                canonical_path="demo_service",
                registration={
                    "head": "0" * 40,
                    "github": {"owner": OWNER, "name": NAME},
                },
            )
        created = client.post(
            "/api/v1/tasks",
            json={
                "title": DEMO_TITLE,
                "description": DEMO_DESCRIPTION,
                "repo_id": str(repository.id),
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        runner = start_runner(runner_dir, timeout=20, poll=0.05)
        async with compiled_worker_graph(database_url) as graph:
            assert await process_one(
                factory,
                provider,
                graph,
                repository_root=repo_root,
                artifacts_root=artifacts_root,
                workspace_root=workspace_root,
                github=options,
            )
            plan = client.get(f"/api/v1/tasks/{task_id}/approvals").json()["items"][0]
            _decide(client, task_id, plan["id"], "approve", "Apply the fix.")
            assert await process_one(
                factory,
                provider,
                graph,
                repository_root=repo_root,
                artifacts_root=artifacts_root,
                workspace_root=workspace_root,
                runner_root=runner_dir,
                runner_wait_seconds=35,
                database_url=database_url,
                github=options,
            )
    finally:
        await engine.dispose()
        if runner is not None:
            stop_runner(runner)
    waiting = client.get(f"/api/v1/tasks/{task_id}")
    assert waiting.json()["status"] == "waiting_approval"
    assert waiting.json()["stage"] == "request_publish_approval"
    return str(task_id), before


async def _finish_publication(
    client: TestClient,
    database_url: str,
    artifacts_root: Path,
    workspace_root: Path,
    tmp_path: Path,
    *,
    transport: httpx2.BaseTransport,
    local_remote: Path,
) -> None:
    del client
    engine, factory = open_session_factory(database_url)
    try:
        async with compiled_worker_graph(database_url) as graph:
            assert await process_one(
                factory,
                FakeProvider(),
                graph,
                repository_root=tmp_path / "repos",
                artifacts_root=artifacts_root,
                workspace_root=workspace_root,
                github=_options(transport, local_remote),
            )
            assert (
                await process_one(
                    factory,
                    FakeProvider(),
                    graph,
                    repository_root=tmp_path / "repos",
                    artifacts_root=artifacts_root,
                    workspace_root=workspace_root,
                    github=_options(transport, local_remote),
                )
                is False
            )
    finally:
        await engine.dispose()


def _options(transport: httpx2.BaseTransport, local_remote: Path) -> GitHubOptions:
    return GitHubOptions(
        token=SecretStr(TOKEN),
        allowed=frozenset({REPO.slug}),
        transport=transport,
        local_remote=local_remote,
    )


def _worktree_head(workspace_root: Path | None, branch: str) -> str:
    assert workspace_root is not None
    task_id = branch.removeprefix("patchpilot/")
    worktree = workspace_root / task_id / "worktree"
    return _git(worktree, "rev-parse", "HEAD")


def _refs(repository: Path) -> list[str]:
    text = _git(repository, "show-ref")
    return [line.split(" ", 1)[1] for line in text.splitlines() if line]


def _config(repository: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), "config", "--list"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _git(repository: Path, *args: str) -> str:
    if not repository.exists() and args[:2] == ("init", "--bare"):
        repository.mkdir()
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 and args[:1] != ("show-ref",):
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()
