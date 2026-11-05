"""The benchmark dataset, its scorer, and one offline fake run."""

import json
from pathlib import Path

import pytest

from patchpilot.benchmark.cases import CASES, REQUIRED_CATEGORIES, cases_for
from patchpilot.benchmark.report import build_document, render_html, render_markdown, write_reports
from patchpilot.benchmark.score import LIMITATIONS, Observation, aggregate, score_case
from patchpilot.llm.fake import INJECTION_TOKEN


def test_dataset_covers_the_required_situations():
    assert 10 <= len(CASES) <= 20
    assert REQUIRED_CATEGORIES <= {case.category for case in CASES}
    assert len({case.id for case in CASES}) == len(CASES)
    assert {case.decision for case in CASES} <= {None, "approve", "reject"}
    assert any(case.fake_only for case in CASES)
    assert any(case.recovery for case in CASES)
    assert any(case.category == "invalid_json" for case in CASES)
    assert any(case.category == "failing_tests" for case in CASES)
    assert any(case.checkout == "injected" for case in CASES)
    live = cases_for("ollama")
    assert live
    assert all(not case.fake_only for case in live)
    assert len(live) < len(CASES)
    assert cases_for("fake") == CASES


def test_scores_are_stable_and_do_not_claim_an_industry_suite(tmp_path: Path):
    case = next(item for item in CASES if item.id == "success-discount")
    observation = _observation(
        status="completed",
        stage="finalize",
        result={
            "classification": "bug",
            "investigation": {
                "rejected_citations": 0,
                "report": {
                    "evidence": [
                        {
                            "relative_path": "src/demo_service/discounts.py",
                            "start_line": 1,
                            "end_line": 1,
                        }
                    ],
                    "hypotheses": [],
                    "probable_files": ["src/demo_service/discounts.py"],
                },
            },
            "plan": {
                "can_proceed": True,
                "paths": ["src/demo_service/discounts.py"],
                "rejected_paths": 0,
            },
            "patch": {
                "checksum": "a" * 64,
                "paths": ["src/demo_service/discounts.py"],
            },
            "tests": {"passed": True},
        },
        checkout=_touch(tmp_path, "src/demo_service/discounts.py"),
    )
    first = score_case(case, observation, require_quality=True)
    second = score_case(case, observation, require_quality=True)
    assert first == second
    assert first.classification_correct is True
    assert first.evidence_valid is True
    assert first.plan_proxy is True
    assert first.patch_applied is True
    assert first.tests_passed is True
    assert first.policy_violation is False
    document = build_document(
        provider="fake",
        model="benchmark-fake",
        results=(first,),
        summary=aggregate((first,)),
    )
    again = json.dumps(document, sort_keys=True)
    assert (
        json.dumps(
            build_document(
                provider="fake",
                model="benchmark-fake",
                results=(second,),
                summary=aggregate((second,)),
            ),
            sort_keys=True,
        )
        == again
    )
    text = render_markdown(document)
    page = render_html(document)
    assert "These scores do not represent industry benchmark quality." in text
    assert "These scores do not represent industry benchmark quality." in page
    assert "SWE-bench" in text
    write_reports(tmp_path / "out", document)
    assert (tmp_path / "out" / "report.json").is_file()
    assert (tmp_path / "out" / "report.md").is_file()
    assert (tmp_path / "out" / "report.html").is_file()
    assert any("industry benchmark" in item for item in LIMITATIONS)


def test_a_leaked_token_or_a_changed_checkout_is_a_policy_violation(tmp_path: Path):
    case = next(item for item in CASES if item.id == "malicious-comment")
    leaked = score_case(
        case,
        _observation(
            status="completed",
            stage="finalize",
            result={"classification": "bug", "error": INJECTION_TOKEN},
            checkout=tmp_path,
        ),
        require_quality=True,
    )
    assert leaked.policy_violation is True
    assert any(check.name == "policy" and check.status == "fail" for check in leaked.checks)

    dirty = score_case(
        case,
        _observation(
            status="completed",
            stage="finalize",
            result={"classification": "bug"},
            checkout=tmp_path,
            source_unchanged=False,
        ),
        require_quality=False,
    )
    assert dirty.policy_violation is True
    assert any(check.required and check.status == "fail" for check in dirty.checks)


def test_cli_rejects_a_missing_database_and_a_cloud_provider(
    monkeypatch: pytest.MonkeyPatch,
):
    from patchpilot.benchmark.__main__ import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["--provider", "fake"]) == 2
    with pytest.raises(SystemExit) as caught:
        main(["--provider", "openai", "--database-url", "postgresql://example"])
    assert caught.value.code == 2


def _observation(
    *,
    status: str,
    stage: str,
    result: dict[str, object],
    checkout: Path,
    source_unchanged: bool = True,
) -> Observation:
    return Observation(
        status=status,
        stage=stage,
        result=result,
        result_summary="",
        event_types=(),
        payload_text="",
        model_outcomes=("success",),
        model_stages=("triage",),
        prompt_tokens=20,
        output_tokens=10,
        tokens_complete=True,
        latency_ms=5,
        observed_classification="bug",
        source_unchanged=source_unchanged,
        checkout=checkout,
    )


def _touch(root: Path, relative: str) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("return KNOWN_TIERS[customer_tier]\n", encoding="utf-8")
    return root


def test_fake_benchmark_cli_is_deterministic(
    postgres_database_url: str,
    clean_tasks: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Run the command the milestone asks for. Fake stays on the fixture provider."""
    from patchpilot.benchmark.__main__ import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    output = tmp_path / "benchmark-results"
    code = main(
        [
            "--provider",
            "fake",
            "--database-url",
            postgres_database_url,
            "--output",
            str(output),
        ]
    )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert code == 0, _failures(report)
    assert report["provider"] == "fake"
    assert report["model"] == "benchmark-fake"
    assert report["summary"]["checks_failed"] == 0
    assert report["summary"]["policy_violations"] == 0
    assert report["summary"]["cases"] == len(CASES)
    assert report["summary"]["skipped"] == 0
    classification = report["summary"]["classification"]
    assert classification["passed"] == classification["scored"]
    assert report["summary"]["prompt_tokens"] is not None
    assert "ollama" not in json.dumps(report["cases"])
    page = (output / "report.html").read_text(encoding="utf-8")
    markdown = (output / "report.md").read_text(encoding="utf-8")
    sentence = "These scores do not represent industry benchmark quality."
    assert sentence in page
    assert sentence in markdown
    assert "http://" not in page
    assert INJECTION_TOKEN not in page
    assert INJECTION_TOKEN not in markdown


def _failures(report: dict[str, object]) -> str:
    lines: list[str] = []
    cases = report["cases"]
    if not isinstance(cases, list):
        return "report has no cases"
    for case in cases:
        if not isinstance(case, dict):
            continue
        failed = [
            check
            for check in case["checks"]
            if isinstance(check, dict) and check["required"] and check["status"] == "fail"
        ]
        if failed:
            lines.append(f"{case['id']}: {failed}")
    return "\n".join(lines) or "checks_failed was set without a failed check"
