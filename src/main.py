"""Async public API health checks and OpenTelemetry instrumentation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import (
    OTLPLogExporter,
)
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.metrics.view import (
    ExplicitBucketHistogramAggregation,
    View,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

from src.config import Settings

logger = logging.getLogger("api_observability")
logger.setLevel(logging.INFO)

LATENCY_BUCKETS_SECONDS = (
    0.05,
    0.1,
    0.2,
    0.3,
    0.5,
    0.75,
    1.0,
    1.5,
    2.0,
    3.0,
    5.0,
    10.0,
)


class ResponseLike(Protocol):
    status_code: int


class AsyncClientLike(Protocol):
    async def get(self, url: str) -> ResponseLike: ...


def _signal_endpoint(base_endpoint: str, signal: str) -> str:
    return f"{base_endpoint}/v1/{signal}"


def _configure_telemetry(
    settings: Settings,
) -> tuple[TracerProvider, MeterProvider, LoggerProvider]:
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "deployment.environment": settings.environment,
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    metric_readers = []
    if settings.otlp_endpoint:
        metric_readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=_signal_endpoint(
                        settings.otlp_endpoint,
                        "metrics",
                    ),
                    headers=settings.otlp_headers,
                    timeout=5,
                ),
                export_interval_millis=60_000,
            )
        )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=metric_readers,
        views=[
            View(
                instrument_name="api_check_duration_seconds",
                aggregation=ExplicitBucketHistogramAggregation(
                    boundaries=LATENCY_BUCKETS_SECONDS,
                ),
            )
        ],
    )
    logger_provider = LoggerProvider(resource=resource)

    if settings.otlp_endpoint:
        base_endpoint = settings.otlp_endpoint
        headers = settings.otlp_headers
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=_signal_endpoint(base_endpoint, "traces"),
                    headers=headers,
                    timeout=5,
                )
            )
        )
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(
                    endpoint=_signal_endpoint(base_endpoint, "logs"),
                    headers=headers,
                    timeout=5,
                )
            )
        )
        logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    else:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is unset; telemetry will not be "
            "exported. Set it for a deployed Lambda."
        )

    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    set_logger_provider(logger_provider)
    return tracer_provider, meter_provider, logger_provider


SETTINGS = Settings.from_env()
TRACER_PROVIDER, METER_PROVIDER, LOGGER_PROVIDER = _configure_telemetry(
    SETTINGS
)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

check_requests = meter.create_counter(
    "api_check_requests_total",
    description="Number of API health checks performed",
)
check_duration = meter.create_histogram(
    "api_check_duration_seconds",
    unit="s",
    description="Duration of API health checks",
)
check_status = meter.create_gauge(
    "api_check_status",
    description="Most recent API check result: 1 for healthy, 0 otherwise",
)
check_last_run = meter.create_gauge(
    "api_check_last_run_timestamp_seconds",
    unit="s",
    description="Unix timestamp of the most recent API check",
)


async def _check_api(
    client: AsyncClientLike,
    semaphore: asyncio.Semaphore,
    url: str,
) -> dict[str, Any]:
    api_name = urlsplit(url).netloc
    started_at = time.perf_counter()
    status_code: int | None = None
    healthy = False
    error_type: str | None = None

    with tracer.start_as_current_span("api.health_check") as span:
        span.set_attribute("server.address", api_name)
        span.set_attribute("http.request.method", "GET")

        try:
            async with semaphore:
                started_at = time.perf_counter()
                response = await client.get(url)

            status_code = response.status_code
            healthy = status_code < 400
            span.set_attribute("http.response.status_code", status_code)
            if not healthy:
                span.set_status(
                    Status(StatusCode.ERROR, "Unhealthy HTTP status")
                )
        except httpx.HTTPError as exc:
            error_type = type(exc).__name__
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, error_type))

        duration = time.perf_counter() - started_at
        result = "healthy" if healthy else "unhealthy"
        check_duration.record(
            duration,
            {"api_target": api_name, "result": result},
        )
        check_requests.add(
            1,
            {
                "api_target": api_name,
                "result": result,
                "status_code": str(status_code) if status_code is not None else "none",
            },
        )
        check_status.set(
            1 if healthy else 0,
            {"api_target": api_name},
        )
        check_last_run.set(
            time.time(),
            {"api_target": api_name},
        )

        log_data = {
            "api": api_name,
            "status_code": status_code,
            "healthy": healthy,
            "latency_ms": round(duration * 1000, 2),
        }
        if not healthy:
            if error_type:
                log_data["error_type"] = error_type
            logger.warning("API health check failed: %s", json.dumps(log_data))
        else:
            logger.info("API health check completed: %s", json.dumps(log_data))

    result_data: dict[str, Any] = {
        "api": api_name,
        "url": url,
        "status_code": status_code,
        "healthy": healthy,
        "latency_ms": round(duration * 1000, 2),
    }
    if error_type:
        result_data["error"] = error_type
    return result_data


async def _run_checks() -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(SETTINGS.max_concurrency)
    async with httpx.AsyncClient(
        timeout=SETTINGS.http_timeout_seconds,
        follow_redirects=True,
    ) as client:
        return await asyncio.gather(
            *(
                _check_api(client, semaphore, url)
                for url in SETTINGS.api_targets
            )
        )


def _flush_telemetry() -> None:
    flush_results = {
        "traces": TRACER_PROVIDER.force_flush(timeout_millis=5_000),
        "metrics": METER_PROVIDER.force_flush(timeout_millis=5_000),
    }
    # The pinned log SDK flushes synchronously but returns None from its batch processor.
    LOGGER_PROVIDER.force_flush(timeout_millis=5_000)
    failed_signals = [
        signal
        for signal, flushed in flush_results.items()
        if not flushed
    ]
    if failed_signals:
        raise RuntimeError(
            "OpenTelemetry flush failed for: " + ", ".join(failed_signals)
        )


def lambda_handler(
    _event: dict[str, Any],
    _context: object,
) -> dict[str, Any]:
    """AWS Lambda entry point invoked by EventBridge."""
    del _event, _context
    try:
        checks = asyncio.run(_run_checks())
        healthy_count = sum(1 for check in checks if check["healthy"])
        summary = {
            "checked": len(checks),
            "healthy": healthy_count,
            "unhealthy": len(checks) - healthy_count,
            "checks": checks,
        }
        return {"statusCode": 200, "body": json.dumps(summary)}
    finally:
        _flush_telemetry()
