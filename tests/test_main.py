import asyncio
import json

import httpx
import pytest

from src import main
from src.config import Settings


class StubResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class StubClient:
    def __init__(
        self,
        response: StubResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error

    async def get(self, _url: str) -> StubResponse:
        assert _url.startswith(("http://", "https://"))
        if self.error:
            raise self.error
        assert self.response is not None
        return self.response


def test_settings_reject_empty_target_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_TARGETS", " , ")

    with pytest.raises(ValueError, match="at least one URL"):
        Settings.from_env()


def test_settings_reject_invalid_otlp_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "malformed")

    with pytest.raises(ValueError, match="key=value"):
        Settings.from_env()


def test_check_api_reports_success() -> None:
    result = asyncio.run(
        main._check_api(
            StubClient(response=StubResponse(200)),
            asyncio.Semaphore(1),
            "https://api.example.test/health",
        )
    )

    assert result["healthy"] is True
    assert result["status_code"] == 200
    assert result["api"] == "api.example.test"
    assert result["latency_ms"] >= 0


def test_check_api_updates_status_and_last_run_gauges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_points: list[tuple[int, dict[str, str]]] = []
    last_run_points: list[tuple[float, dict[str, str]]] = []
    monkeypatch.setattr(
        main.check_status,
        "set",
        lambda value, attributes: status_points.append((value, attributes)),
    )
    monkeypatch.setattr(
        main.check_last_run,
        "set",
        lambda value, attributes: last_run_points.append((value, attributes)),
    )

    asyncio.run(
        main._check_api(
            StubClient(response=StubResponse(200)),
            asyncio.Semaphore(1),
            "https://api.example.test/health",
        )
    )

    assert status_points == [(1, {"api_target": "api.example.test"})]
    assert len(last_run_points) == 1
    assert last_run_points[0][0] > 0
    assert last_run_points[0][1] == {"api_target": "api.example.test"}


def test_latency_histogram_has_boundaries_around_alert_threshold() -> None:
    assert 0.5 in main.LATENCY_BUCKETS_SECONDS
    assert 0.3 in main.LATENCY_BUCKETS_SECONDS
    assert 0.75 in main.LATENCY_BUCKETS_SECONDS


def test_status_metrics_are_gauges() -> None:
    assert main.check_status.name == "api_check_status"
    assert main.check_last_run.name == "api_check_last_run_timestamp_seconds"


def test_check_api_reports_unhealthy_http_status() -> None:
    result = asyncio.run(
        main._check_api(
            StubClient(response=StubResponse(503)),
            asyncio.Semaphore(1),
            "https://api.example.test/health",
        )
    )

    assert result["healthy"] is False
    assert result["status_code"] == 503


def test_check_api_reports_transport_error() -> None:
    result = asyncio.run(
        main._check_api(
            StubClient(error=httpx.ConnectError("connection refused")),
            asyncio.Semaphore(1),
            "https://api.example.test/health",
        )
    )

    assert result["healthy"] is False
    assert result["status_code"] is None
    assert result["error"] == "ConnectError"


def test_lambda_handler_returns_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main,
        "_run_checks",
        lambda: asyncio.sleep(
            0,
            result=[
                {"healthy": True, "api": "a.example"},
                {"healthy": False, "api": "b.example"},
            ],
        ),
    )
    monkeypatch.setattr(main, "_flush_telemetry", lambda: None)

    response = main.lambda_handler({}, object())
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["checked"] == 2
    assert body["healthy"] == 1
    assert body["unhealthy"] == 1


def test_flush_telemetry_does_not_treat_log_flush_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SuccessfulProvider:
        def force_flush(self, timeout_millis: int) -> bool:
            assert timeout_millis == 5_000
            return True

    class LogProvider:
        def __init__(self) -> None:
            self.flushed = False

        def force_flush(self, timeout_millis: int) -> bool:
            assert timeout_millis == 5_000
            self.flushed = True
            return False

    log_provider = LogProvider()
    monkeypatch.setattr(main, "TRACER_PROVIDER", SuccessfulProvider())
    monkeypatch.setattr(main, "METER_PROVIDER", SuccessfulProvider())
    monkeypatch.setattr(main, "LOGGER_PROVIDER", log_provider)

    main._flush_telemetry()

    assert log_provider.flushed is True
