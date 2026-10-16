"""Environment-based application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


DEFAULT_API_TARGETS = (
    "https://api.github.com,"
    "https://httpbin.org/status/200"
)


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _parse_headers(raw_headers: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw_headers.split(","):
        if not item.strip():
            continue
        key, separator, value = item.partition("=")
        key = unquote(key.strip())
        value = unquote(value.strip())
        if not separator or not key or not value:
            raise ValueError(
                "OTEL_EXPORTER_OTLP_HEADERS must contain comma-separated "
                "key=value pairs"
            )
        if key in headers:
            raise ValueError(
                f"OTEL_EXPORTER_OTLP_HEADERS contains duplicate key {key!r}"
            )
        headers[key] = value
    return headers


@dataclass(frozen=True)
class Settings:
    """Validated settings used by the Lambda handler."""

    api_targets: tuple[str, ...]
    http_timeout_seconds: float
    max_concurrency: int
    otlp_endpoint: str | None
    otlp_headers: dict[str, str]
    service_name: str
    environment: str

    @classmethod
    def from_env(cls) -> Settings:
        raw_targets = os.getenv("API_TARGETS", DEFAULT_API_TARGETS)
        targets = tuple(
            target.strip()
            for target in raw_targets.split(",")
            if target.strip()
        )
        if not targets:
            raise ValueError("API_TARGETS must contain at least one URL")

        for target in targets:
            parsed = urlsplit(target)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"Invalid API target URL: {target!r}")

        raw_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        endpoint = raw_endpoint.rstrip("/") or None
        if endpoint:
            parsed_endpoint = urlsplit(endpoint)
            if (
                parsed_endpoint.scheme not in {"http", "https"}
                or not parsed_endpoint.netloc
            ):
                raise ValueError(
                    "OTEL_EXPORTER_OTLP_ENDPOINT must be an HTTP(S) URL"
                )

        return cls(
            api_targets=targets,
            http_timeout_seconds=_positive_float(
                "HTTP_TIMEOUT_SECONDS",
                10.0,
            ),
            max_concurrency=_positive_int("API_CONCURRENCY", 10),
            otlp_endpoint=endpoint,
            otlp_headers=_parse_headers(
                os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
            ),
            service_name=os.getenv(
                "OTEL_SERVICE_NAME",
                "serverless-api-observability",
            ),
            environment=os.getenv("DEPLOYMENT_ENVIRONMENT", "production"),
        )
