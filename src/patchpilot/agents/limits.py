"""Hard ceilings for tool calls, repairs, and supervisor routing.

A setting or a caller can choose a smaller budget. Values above the ceiling are
rejected so the limit cannot be turned off. A task may repair a patch at most
twice. The supervisor may investigate twice and plan twice, and the graph stops
if it keeps routing or runs past its deadline.
"""

import math

DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS = 6
HARD_MAX_INVESTIGATOR_TOOL_CALLS = 12
DEFAULT_MAX_REPAIRS = 2
HARD_MAX_REPAIRS = 2
MAX_INVESTIGATIONS = 2
MAX_PLANS = 2
MAX_SUPERVISOR_STEPS = 24
GRAPH_RECURSION_LIMIT = 40
DEFAULT_SUPERVISOR_DEADLINE_SECONDS = 900.0
MAX_SUPERVISOR_DEADLINE_SECONDS = 3600.0


def require_tool_budget(value: int) -> int:
    """Return the budget, or raise when it is not a small positive integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max tool calls must be an integer")
    if not 1 <= value <= HARD_MAX_INVESTIGATOR_TOOL_CALLS:
        raise ValueError(f"max tool calls must be from 1 to {HARD_MAX_INVESTIGATOR_TOOL_CALLS}")
    return value


def require_repair_budget(value: int) -> int:
    """Return the repair cap. It cannot be raised above two."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max repairs must be an integer")
    if not 0 <= value <= HARD_MAX_REPAIRS:
        raise ValueError(f"max repairs must be from 0 to {HARD_MAX_REPAIRS}")
    return value


def require_supervisor_deadline(value: float) -> float:
    """Return the routing deadline. It cannot be turned off or extended past an hour."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("supervisor deadline must be a number of seconds")
    deadline = float(value)
    if not math.isfinite(deadline) or deadline < 1 or deadline > MAX_SUPERVISOR_DEADLINE_SECONDS:
        raise ValueError("supervisor deadline must be from 1 to 3600 seconds")
    return deadline
