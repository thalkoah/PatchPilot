"""Agent control-flow signals. These are not stored on the task."""


class StopForLease(Exception):
    """The worker lost the lease. The node must not checkpoint a result."""
