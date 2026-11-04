"""Restricted test runner.

The worker publishes a job id on the shared volume. This package's `__main__`
is the process that runs pytest. The worker module does not import it.
"""
