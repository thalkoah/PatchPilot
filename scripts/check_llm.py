#!/usr/bin/env python3
"""Print one schema-valid TriageResult.

The default provider is fake and stays offline. Point LLM_PROVIDER at ollama
for a manual smoke test against a local Ollama server. A failed response
exits non-zero and the message does not include model text.
"""

import argparse
import os
import sys

from pydantic import ValidationError

from patchpilot.config import LLMSettings
from patchpilot.llm.errors import LLMError
from patchpilot.llm.factory import create_provider
from patchpilot.llm.triage import TriageResult

DEMO_PROMPT = (
    "Title: Unknown customer tier crashes discount endpoint\n"
    "Description: When the demo-service discount endpoint receives a customer tier "
    "it does not recognize, it returns HTTP 500. It should respond with HTTP 422 "
    "and a clear validation error. Find the cause, propose a minimal fix and add "
    "a regression test."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("fake", "ollama"))
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--prompt", default=DEMO_PROMPT)
    args = parser.parse_args(argv)
    overrides = {}
    if args.provider is not None:
        overrides["LLM_PROVIDER"] = args.provider
    if args.model is not None:
        overrides["LLM_MODEL"] = args.model
    if args.base_url is not None:
        overrides["OLLAMA_BASE_URL"] = args.base_url
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        return _run(args.prompt)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run(prompt: str) -> int:
    try:
        settings = LLMSettings()
    except ValidationError as exc:
        missing = [str(error["loc"][-1]) for error in exc.errors() if error["type"] == "missing"]
        if missing:
            print(f"error: missing settings: {', '.join(missing)}", file=sys.stderr)
        else:
            print("error: invalid LLM settings", file=sys.stderr)
        return 2

    provider = create_provider(settings)
    try:
        result = provider.generate_structured(prompt, TriageResult)
    except (LLMError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        provider.close()

    checked = TriageResult.model_validate(result.model_dump())
    if settings.llm_provider == "fake":
        print("provider=fake", file=sys.stderr)
    else:
        print(
            f"provider=ollama model={settings.llm_model} base_url={settings.ollama_base_url}",
            file=sys.stderr,
        )
    print(checked.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
