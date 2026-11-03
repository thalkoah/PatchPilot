"""Local admin commands for the repository registry.

There is no HTTP registration route. The API has no authentication, and a
request forwarded into the container is not treated as an administrator.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

from patchpilot.config import get_settings
from patchpilot.db.session import create_engine, create_session_factory
from patchpilot.repositories.repos import RepoRepository
from patchpilot.source.errors import SourceError
from patchpilot.source.registry import register_checkout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m patchpilot.source")
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register", help="register a checkout under REPOSITORY_ROOT")
    register.add_argument("--name", required=True, help="display name stored with the checkout")
    register.add_argument("--path", required=True, help="path relative to REPOSITORY_ROOT")

    commands.add_parser("list", help="list registered checkouts")

    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            return asyncio.run(_register(args.name, args.path))
        return asyncio.run(_list())
    except SourceError as exc:
        print(exc.code, file=sys.stderr)
        return 2 if exc.code == "root_missing" else 1
    except ValidationError as exc:
        message = "invalid configuration"
        if exc.errors():
            message = str(exc.errors()[0].get("msg", message))
        print(message, file=sys.stderr)
        return 2


async def _register(display_name: str, relative_path: str) -> int:
    settings = get_settings()
    root = _require_root(settings.repository_root)
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            row = await register_checkout(
                RepoRepository(session),
                repository_root=root,
                relative_path=relative_path,
                display_name=display_name,
            )
    finally:
        await engine.dispose()
    print(f"{row.id} {row.canonical_path}")
    return 0


async def _list() -> int:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            rows = await RepoRepository(session).list_all()
    finally:
        await engine.dispose()
    for row in rows:
        state = "enabled" if row.enabled else "disabled"
        print(f"{row.id} {state} {row.canonical_path}")
    return 0


def _require_root(value: str | None) -> Path:
    if value is None:
        raise SourceError("root_missing")
    return Path(value)


if __name__ == "__main__":
    raise SystemExit(main())
