"""Bounds for the restricted test runner.

The runner executes one fixed pytest command. These limits are not taken from
model output. A shared runner volume is weaker isolation than a fresh mount per
task, so the prototype stays single-user and the worker concurrency stays 1.
"""

DEFAULT_RUNNER_TIMEOUT_SECONDS = 30.0
DEFAULT_RUNNER_WAIT_SECONDS = 45.0
DEFAULT_RUNNER_POLL_SECONDS = 0.2
MAX_RUNNER_TIMEOUT_SECONDS = 120.0
MAX_RUNNER_WAIT_SECONDS = 180.0
MAX_RUNNER_POLL_SECONDS = 5.0
MAX_OUTPUT_CHARS = 2_000
MAX_CAPTURE_BYTES = 2_000
MAX_DRAIN_BYTES = 256 * 1024
MAX_REPORT_BYTES = 16_384
MAX_COPY_FILES = 200
MAX_COPY_BYTES = 1_048_576
MAX_FILE_BYTES = 131_072
SCHEMA_VERSION = 1
