"""Hard ceilings for investigator tool calls and review repairs.

A setting or a caller can choose a smaller budget. Values above the ceiling are
rejected so the limit cannot be turned off. A task may repair a patch at most
twice.
"""

DEFAULT_INVESTIGATOR_MAX_TOOL_CALLS = 6
HARD_MAX_INVESTIGATOR_TOOL_CALLS = 12
DEFAULT_MAX_REPAIRS = 2
HARD_MAX_REPAIRS = 2


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
