"""Create the example Git checkouts used by repository registration.

The parent repository ignores `examples/demo_service/.git/` and
`examples/minitask/.git/`. Re-running this script does nothing for a checkout
whose `.git` directory already exists.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_AUTHOR_NAME = "PatchPilot Demo"
_AUTHOR_EMAIL = "demo@example.invalid"

# (checkout, commit message, already-initialized notice, created notice)
CHECKOUTS = (
    (
        ROOT / "examples" / "demo_service",
        "Baseline demo service with the unknown-tier defect.",
        "demo repository already initialized",
        "initialized examples/demo_service",
    ),
    (
        ROOT / "examples" / "minitask",
        "Baseline MiniTask task board.",
        "minitask repository already initialized",
        "initialized examples/minitask",
    ),
)


def main() -> int:
    git = shutil.which("git")
    if git is None:
        print("git is required to initialize the example checkouts", file=sys.stderr)
        return 1
    for checkout, message, already, created in CHECKOUTS:
        _init_checkout(git, checkout, message, already, created)
    return 0


def _init_checkout(
    git: str,
    checkout: Path,
    message: str,
    already: str,
    created: str,
) -> None:
    if (checkout / ".git").exists():
        print(already)
        return
    subprocess.run([git, "init", "-b", "main", str(checkout)], check=True)
    subprocess.run([git, "-C", str(checkout), "add", "--", "."], check=True)
    subprocess.run(
        [
            git,
            "-C",
            str(checkout),
            "-c",
            f"user.name={_AUTHOR_NAME}",
            "-c",
            f"user.email={_AUTHOR_EMAIL}",
            "commit",
            "-m",
            message,
        ],
        check=True,
    )
    print(created)


if __name__ == "__main__":
    raise SystemExit(main())
