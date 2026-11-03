"""Create the demo service Git checkout used by repository registration.

The parent repository ignores `examples/demo_service/.git/`. Re-running this
script does nothing once that directory exists.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo_service"


def main() -> int:
    git = shutil.which("git")
    if git is None:
        print("git is required to initialize examples/demo_service", file=sys.stderr)
        return 1
    if (DEMO / ".git").exists():
        print("demo repository already initialized")
        return 0
    subprocess.run([git, "init", "-b", "main", str(DEMO)], check=True)
    subprocess.run([git, "-C", str(DEMO), "add", "--", "."], check=True)
    subprocess.run(
        [
            git,
            "-C",
            str(DEMO),
            "-c",
            "user.name=PatchPilot Demo",
            "-c",
            "user.email=demo@example.invalid",
            "commit",
            "-m",
            "Baseline demo service with the unknown-tier defect.",
        ],
        check=True,
    )
    print("initialized examples/demo_service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
