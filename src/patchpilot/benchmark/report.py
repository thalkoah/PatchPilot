"""JSON, Markdown, and HTML for one benchmark run. All three use the same numbers."""

import html
import json
from pathlib import Path
from typing import Any

from patchpilot.benchmark.score import LIMITATIONS, CaseResult


def build_document(
    *,
    provider: str,
    model: str,
    results: tuple[CaseResult, ...],
    summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "harness": "patchpilot-benchmark",
        "version": 1,
        "provider": provider,
        "model": model,
        "limitations": list(LIMITATIONS),
        "summary": summary,
        "cases": [_case(item) for item in results],
    }


def write_reports(directory: Path, document: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (directory / "report.md").write_text(render_markdown(document), encoding="utf-8")
    (directory / "report.html").write_text(render_html(document), encoding="utf-8")


def render_markdown(document: dict[str, Any]) -> str:
    summary = document["summary"]
    lines = [
        "# PatchPilot benchmark",
        "",
        f"Provider `{document['provider']}`, model `{document['model']}`.",
        "",
        _rate_line("Classification", summary["classification"]),
        _rate_line("Valid evidence", summary["evidence_valid"]),
        _rate_line("Plan proxy", summary["plan_proxy"]),
        _count_line("Patch apply", summary["patch_apply"], "applied", "attempted"),
        _count_line("Tests", summary["test_pass"], "passed", "ran"),
        f"Policy violations: {summary['policy_violations']}.",
        f"Retries (repairs and lease retries): {summary['retries']}.",
        f"Invalid model outputs: {summary['invalid_outputs']}.",
        f"Latency: {summary['latency_ms']} ms.",
        _token_line(summary),
        (
            f"Required checks failed: {summary['checks_failed']} "
            f"across {summary['cases_failed']} cases "
            f"({summary['ran']} ran, {summary['skipped']} skipped)."
        ),
        "",
        "## Cases",
        "",
        (
            "| Case | Category | Status | Class | Evidence "
            "| Plan | Patch | Tests | Policy | Retries |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in document["cases"]:
        lines.append(
            "| {id} | {category} | {status} | {classification} | {evidence} | {plan} | "
            "{patch} | {tests} | {policy} | {retries} |".format(
                id=case["id"],
                category=case["category"],
                status=_cell(case),
                classification=_mark(case["classification_correct"]),
                evidence=_mark(case["evidence_valid"]),
                plan=_mark(case["plan_proxy"]),
                patch=_patch_cell(case),
                tests=_tests_cell(case),
                policy="violation" if case["policy_violation"] else "ok",
                retries=case["retries"],
            )
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in document["limitations"])
    lines.append("")
    return "\n".join(lines)


def render_html(document: dict[str, Any]) -> str:
    summary = document["summary"]
    rows = "\n".join(_html_row(case) for case in document["cases"])
    limits = "\n".join(f"<li>{html.escape(item)}</li>" for item in document["limitations"])
    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PatchPilot benchmark</title>
<style>
body {{ font: 16px/1.45 system-ui, sans-serif; margin: 1.25rem; color: #1c1917; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid #d6d3d1; padding: 0.35rem; text-align: left; }}
.wrap {{ overflow-x: auto; }}
.fail {{ color: #9f1239; }}
</style>
</head>
<body>
<h1>PatchPilot benchmark</h1>
<p>Provider {html.escape(str(document["provider"]))},
model {html.escape(str(document["model"]))}.</p>
<ul>
<li>{html.escape(_rate_line("Classification", summary["classification"]))}</li>
<li>{html.escape(_rate_line("Valid evidence", summary["evidence_valid"]))}</li>
<li>{html.escape(_rate_line("Plan proxy", summary["plan_proxy"]))}</li>
<li>{html.escape(_count_line("Patch apply", summary["patch_apply"], "applied", "attempted"))}</li>
<li>{html.escape(_count_line("Tests", summary["test_pass"], "passed", "ran"))}</li>
<li>Policy violations: {summary["policy_violations"]}.</li>
<li>Retries (repairs and lease retries): {summary["retries"]}.</li>
<li>Invalid model outputs: {summary["invalid_outputs"]}.</li>
<li>Latency: {summary["latency_ms"]} ms.</li>
<li>{html.escape(_token_line(summary))}</li>
<li>Required checks failed: {summary["checks_failed"]} across {summary["cases_failed"]} cases
({summary["ran"]} ran, {summary["skipped"]} skipped).</li>
</ul>
<div class="wrap">
<table>
<thead>
<tr>
<th>Case</th><th>Category</th><th>Status</th><th>Classification</th><th>Evidence</th>
<th>Plan</th><th>Patch</th><th>Tests</th><th>Policy</th><th>Retries</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</div>
<h2>Limitations</h2>
<ul>
{limits}
</ul>
</body>
</html>
"""
    return body


def _case(item: CaseResult) -> dict[str, Any]:
    return {
        "id": item.case_id,
        "category": item.category,
        "title": item.title,
        "skipped": item.skipped,
        "skip_reason": item.skip_reason,
        "status": item.status,
        "stage": item.stage,
        "summary": item.summary,
        "checks": [
            {
                "name": check.name,
                "status": check.status,
                "required": check.required,
                "detail": check.detail,
            }
            for check in item.checks
        ],
        "classification_correct": item.classification_correct,
        "evidence_valid": item.evidence_valid,
        "evidence_present": item.evidence_present,
        "plan_proxy": item.plan_proxy,
        "patch_attempted": item.patch_attempted,
        "patch_applied": item.patch_applied,
        "tests_passed": item.tests_passed,
        "policy_violation": item.policy_violation,
        "retries": item.retries,
        "invalid_outputs": item.invalid_outputs,
        "latency_ms": item.latency_ms,
        "prompt_tokens": item.prompt_tokens,
        "output_tokens": item.output_tokens,
        "tokens_complete": item.tokens_complete,
    }


def _rate_line(label: str, block: dict[str, Any]) -> str:
    rate = block["rate"]
    shown = "n/a" if rate is None else f"{rate:.0%}"
    return f"{label}: {block['passed']}/{block['scored']} ({shown})."


def _count_line(label: str, block: dict[str, Any], numerator: str, denominator: str) -> str:
    rate = block["rate"]
    shown = "n/a" if rate is None else f"{rate:.0%}"
    return f"{label}: {block[numerator]}/{block[denominator]} ({shown})."


def _token_line(summary: dict[str, Any]) -> str:
    prompt = summary["prompt_tokens"]
    output = summary["output_tokens"]
    prompt_text = "unknown" if prompt is None else str(prompt)
    output_text = "unknown" if output is None else str(output)
    complete = "complete" if summary["tokens_complete"] else "partial or unknown"
    return f"Reported tokens: prompt {prompt_text}, output {output_text} ({complete})."


def _mark(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "n/a"


def _cell(case: dict[str, Any]) -> str:
    if case["skipped"]:
        return "skipped"
    return str(case["status"])


def _patch_cell(case: dict[str, Any]) -> str:
    if not case["patch_attempted"]:
        return "n/a"
    return "applied" if case["patch_applied"] else "refused"


def _tests_cell(case: dict[str, Any]) -> str:
    if case["tests_passed"] is None:
        return "n/a"
    return "passed" if case["tests_passed"] else "failed"


def _html_row(case: dict[str, Any]) -> str:
    values = (
        case["id"],
        case["category"],
        _cell(case),
        _mark(case["classification_correct"]),
        _mark(case["evidence_valid"]),
        _mark(case["plan_proxy"]),
        _patch_cell(case),
        _tests_cell(case),
        "violation" if case["policy_violation"] else "ok",
        str(case["retries"]),
    )
    cells = "".join(f"<td>{html.escape(value)}</td>" for value in values)
    failed = case["policy_violation"] or any(
        check["required"] and check["status"] == "fail" for check in case["checks"]
    )
    klass = ' class="fail"' if failed else ""
    return f"<tr{klass}>{cells}</tr>"
