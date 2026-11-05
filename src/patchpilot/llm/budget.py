"""Per-task ceilings. A caller can choose less. It cannot turn a ceiling off."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from patchpilot.agents.limits import DEFAULT_MAX_REPAIRS, HARD_MAX_REPAIRS
from patchpilot.llm.errors import LLMBudgetError, LLMCircuitError

DEFAULT_MAX_MODEL_CALLS = 24
HARD_MAX_MODEL_CALLS = 48
DEFAULT_MAX_TASK_TOOL_CALLS = 12
HARD_MAX_TASK_TOOL_CALLS = 24
DEFAULT_MAX_TOKENS = 100_000
HARD_MAX_TOKENS = 500_000
DEFAULT_MAX_RUNTIME_SECONDS = 900.0
HARD_MAX_RUNTIME_SECONDS = 3600.0
DEFAULT_MAX_PARALLELISM = 1
HARD_MAX_PARALLELISM = 1
INVALID_OUTPUT_LIMIT = 2

_CallKey = tuple[str, str, str]


def require_model_call_budget(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max model calls must be an integer")
    if not 1 <= value <= HARD_MAX_MODEL_CALLS:
        raise ValueError(f"max model calls must be from 1 to {HARD_MAX_MODEL_CALLS}")
    return value


def require_task_tool_budget(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max tool calls must be an integer")
    if not 1 <= value <= HARD_MAX_TASK_TOOL_CALLS:
        raise ValueError(f"max tool calls must be from 1 to {HARD_MAX_TASK_TOOL_CALLS}")
    return value


def require_token_budget(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max tokens must be an integer")
    if not 1 <= value <= HARD_MAX_TOKENS:
        raise ValueError(f"max tokens must be from 1 to {HARD_MAX_TOKENS}")
    return value


def require_runtime_budget(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("max runtime must be a number of seconds")
    runtime = float(value)
    if runtime < 1 or runtime > HARD_MAX_RUNTIME_SECONDS:
        raise ValueError("max runtime must be from 1 to 3600 seconds")
    return runtime


def require_parallelism(value: int) -> int:
    """Local runs stay at one model call at a time. The cap cannot be raised."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max parallelism must be an integer")
    if not 1 <= value <= HARD_MAX_PARALLELISM:
        raise ValueError(f"max parallelism must be from 1 to {HARD_MAX_PARALLELISM}")
    return value


@dataclass
class BudgetSeed:
    """Counts already stored for this task. A new process does not start at zero."""

    model_calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    tokens_reported: bool = False
    invalid_streak: dict[_CallKey, int] = field(default_factory=dict)


@dataclass
class TaskBudget:
    """Counters for one task. The lock covers the in-flight slots."""

    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS
    max_tool_calls: int = DEFAULT_MAX_TASK_TOOL_CALLS
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS
    max_parallelism: int = DEFAULT_MAX_PARALLELISM
    max_repairs: int = DEFAULT_MAX_REPAIRS
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    tokens_reported: bool = False
    invalid_streak: dict[_CallKey, int] = field(default_factory=dict)
    _in_flight: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.max_model_calls = require_model_call_budget(self.max_model_calls)
        self.max_tool_calls = require_task_tool_budget(self.max_tool_calls)
        self.max_tokens = require_token_budget(self.max_tokens)
        self.max_runtime_seconds = require_runtime_budget(self.max_runtime_seconds)
        self.max_parallelism = require_parallelism(self.max_parallelism)
        if not 0 <= self.max_repairs <= HARD_MAX_REPAIRS:
            raise ValueError(f"max repairs must be from 0 to {HARD_MAX_REPAIRS}")

    @classmethod
    def from_seed(
        cls,
        seed: BudgetSeed,
        *,
        max_model_calls: int = DEFAULT_MAX_MODEL_CALLS,
        max_tool_calls: int = DEFAULT_MAX_TASK_TOOL_CALLS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
        max_parallelism: int = DEFAULT_MAX_PARALLELISM,
        max_repairs: int = DEFAULT_MAX_REPAIRS,
        clock: Callable[[], float] = time.monotonic,
    ) -> "TaskBudget":
        return cls(
            max_model_calls=max_model_calls,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
            max_runtime_seconds=max_runtime_seconds,
            max_parallelism=max_parallelism,
            max_repairs=max_repairs,
            clock=clock,
            model_calls=seed.model_calls,
            prompt_tokens=seed.prompt_tokens,
            output_tokens=seed.output_tokens,
            tokens_reported=seed.tokens_reported,
            invalid_streak=dict(seed.invalid_streak),
        )

    def check_runtime(self) -> None:
        if self.started_at is None:
            self.started_at = self.clock()
        if self.clock() - self.started_at > self.max_runtime_seconds:
            raise LLMBudgetError("task runtime budget is exhausted")

    def ensure_circuit(self, stage: str, provider: str, model: str) -> None:
        key = (stage, provider, model)
        if self.invalid_streak.get(key, 0) >= INVALID_OUTPUT_LIMIT:
            raise LLMCircuitError("model output circuit is open")

    def begin_model_call(self) -> None:
        """Reserve one in-flight slot, or refuse before the provider is called."""
        self.check_runtime()
        if self.tokens_reported and self.prompt_tokens + self.output_tokens > self.max_tokens:
            raise LLMBudgetError("token budget is exhausted")
        with self._lock:
            if self.model_calls >= self.max_model_calls:
                raise LLMBudgetError("model call budget is exhausted")
            if self._in_flight >= self.max_parallelism:
                raise LLMBudgetError("model parallelism budget is exhausted")
            self._in_flight += 1

    def finish_model_call(
        self,
        *,
        stage: str,
        provider: str,
        model: str,
        prompt_tokens: int | None,
        output_tokens: int | None,
        invalid_output: bool,
        succeeded: bool,
    ) -> None:
        """Release the slot and count the call. Tokens count only when reported.

        A successful call clears the invalid-output streak. A timeout or a
        transport failure does not, and it does not open the circuit either.
        """
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self.model_calls += 1
            key = (stage, provider, model)
            if invalid_output:
                self.invalid_streak[key] = self.invalid_streak.get(key, 0) + 1
            elif succeeded:
                self.invalid_streak[key] = 0
            if prompt_tokens is not None:
                self.tokens_reported = True
                self.prompt_tokens += prompt_tokens
            if output_tokens is not None:
                self.tokens_reported = True
                self.output_tokens += output_tokens
        self._check_tokens()

    def abort_model_call(self) -> None:
        """Release a slot when the call was refused before the provider ran."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    def charge_tool(self) -> None:
        self.check_runtime()
        if self.tool_calls >= self.max_tool_calls:
            raise LLMBudgetError("tool call budget is exhausted")
        self.tool_calls += 1

    def allow_repair(self, repair_count: int) -> None:
        """Refuse a repair past the cap. The first develop pass is not a repair."""
        if isinstance(repair_count, bool) or not isinstance(repair_count, int):
            raise LLMBudgetError("repair budget is exhausted")
        if repair_count > self.max_repairs:
            raise LLMBudgetError("repair budget is exhausted")

    def _check_tokens(self) -> None:
        if not self.tokens_reported:
            return
        if self.prompt_tokens + self.output_tokens > self.max_tokens:
            raise LLMBudgetError("token budget is exhausted")
