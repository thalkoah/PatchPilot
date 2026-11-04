"""Failures of the job protocol. Messages stay off the task record."""


class RunnerRejected(Exception):
    """The workspace or the request was refused. Pytest was not the worker."""


class RunnerUnavailable(Exception):
    """No result file appeared before the wait elapsed."""
