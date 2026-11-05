"""Offline benchmark. Fake is the default and does not call a model service.

    uv run python -m patchpilot.benchmark --provider fake --database-url "$DATABASE_URL"

`--provider ollama` is opt-in. It uses the configured local model on the same
issue texts and skips faults that only the fake fixtures can force. A cloud
provider is not a choice here.
"""

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError

from patchpilot.benchmark.report import write_reports
from patchpilot.benchmark.run import BenchmarkError, run_benchmark
from patchpilot.config import get_settings
from patchpilot.logconfig import configure_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("fake", "ollama"),
        default="fake",
        help="fake is offline and deterministic; ollama is an explicit local-model run",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="migrated PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmark-results"),
        help="directory for report.json, report.md, and report.html",
    )
    args = parser.parse_args(argv)
    if not str(args.database_url).strip():
        print("error: set DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2
    configure_logging("WARNING")
    logging.getLogger("patchpilot.benchmark").setLevel(logging.INFO)
    settings = None
    if args.provider == "ollama":
        settings = _ollama_settings(args.database_url)
        if settings is None:
            return 2
    try:
        with tempfile.TemporaryDirectory(prefix="patchpilot-benchmark-") as raw:
            finished = asyncio.run(
                run_benchmark(
                    provider=args.provider,
                    database_url=args.database_url,
                    work_root=Path(raw),
                    ollama_settings=settings,
                )
            )
    except BenchmarkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: benchmark failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    write_reports(args.output, finished.document)
    _print_summary(finished.document, args.output)
    return 0 if finished.ok else 1


def _ollama_settings(database_url: str) -> object | None:
    """Load model settings. The harness still uses the database URL from the CLI."""
    if not os.environ.get("DATABASE_URL", "").strip():
        os.environ["DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        return get_settings()
    except ValidationError:
        print("error: invalid settings", file=sys.stderr)
        return None


def _print_summary(document: dict[str, object], output: Path) -> None:
    summary = document["summary"]
    if not isinstance(summary, dict):
        return
    print(
        f"provider={document['provider']} model={document['model']} "
        f"cases={summary['ran']} skipped={summary['skipped']} "
        f"checks_failed={summary['checks_failed']}"
    )
    print(
        "classification={passed}/{scored} evidence={evidence} plan={plan} "
        "patch={applied}/{attempted} tests={tests_passed}/{tests_ran} "
        "policy_violations={policy} retries={retries} "
        "invalid_outputs={invalid} latency_ms={latency}".format(
            passed=summary["classification"]["passed"],
            scored=summary["classification"]["scored"],
            evidence=_ratio(summary["evidence_valid"]),
            plan=_ratio(summary["plan_proxy"]),
            applied=summary["patch_apply"]["applied"],
            attempted=summary["patch_apply"]["attempted"],
            tests_passed=summary["test_pass"]["passed"],
            tests_ran=summary["test_pass"]["ran"],
            policy=summary["policy_violations"],
            retries=summary["retries"],
            invalid=summary["invalid_outputs"],
            latency=summary["latency_ms"],
        )
    )
    print(f"wrote {output / 'report.json'}")
    print("These scores do not represent industry benchmark quality.")


def _ratio(block: object) -> str:
    if not isinstance(block, dict):
        return "n/a"
    return f"{block['passed']}/{block['scored']}"


if __name__ == "__main__":
    raise SystemExit(main())
