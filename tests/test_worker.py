import logging

from patchpilot.config import get_settings
from patchpilot.worker import main


def test_worker_stays_idle_and_does_not_open_database(monkeypatch, caplog):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://patchpilot:patchpilot_dev@db:5432/patchpilot",
    )
    get_settings.cache_clear()

    def fail_connect(*args, **kwargs):
        raise AssertionError("idle worker must not open a database connection")

    def stop_sleep(seconds):
        assert seconds == 30.0
        raise KeyboardInterrupt

    monkeypatch.setattr("psycopg.connect", fail_connect)
    monkeypatch.setattr("patchpilot.worker.time.sleep", stop_sleep)

    with caplog.at_level(logging.INFO):
        main()

    assert "worker idle; no jobs configured" in caplog.text
    assert "worker stopped" in caplog.text
