"""OpenTelemetry setup for RMP API, worker, and activities."""
import logging
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Dict, Iterator, Optional

logger = logging.getLogger("rmp.telemetry")

_initialized = False
_tracer = None
_status: Dict[str, Any] = {
    "enabled": False,
    "ready": False,
    "service_name": "rmp",
    "otlp_endpoint": "",
    "console_export": False,
    "fastapi_instrumented": False,
    "temporal_tracing": False,
    "error": None,
}

TRACE_ATTR_KEYS = (
    "task_id",
    "process_run_id",
    "step_name",
    "observation_type",
    "memory_type",
    "scope_type",
    "scope_id",
    "event_type",
    "status",
    "correlation_id",
)


def _telemetry_config() -> Dict[str, Any]:
    from app.config import get_telemetry_config

    return get_telemetry_config()


def is_telemetry_enabled() -> bool:
    return bool(_telemetry_config().get("enabled", False))


def _extract_attrs(payload: Any) -> Dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    attrs: Dict[str, str] = {}
    for key in TRACE_ATTR_KEYS:
        val = payload.get(key)
        if val is not None and str(val).strip():
            attrs[f"rmp.{key}"] = str(val)[:128]
    if payload.get("session_key"):
        attrs["rmp.session_key"] = str(payload["session_key"])[-32:]
    if payload.get("intent"):
        attrs["rmp.intent_preview"] = str(payload["intent"])[:120]
    return attrs


def init_telemetry(service_name: Optional[str] = None) -> Dict[str, Any]:
    """Initialize tracer provider and exporters once per process."""
    global _initialized, _tracer, _status

    cfg = _telemetry_config()
    name = service_name or cfg.get("service_name", "rmp")
    _status.update(
        {
            "enabled": bool(cfg.get("enabled", False)),
            "service_name": name,
            "otlp_endpoint": cfg.get("otlp_endpoint", "") or "",
            "console_export": bool(cfg.get("console_export", False)),
        }
    )

    if not cfg.get("enabled", False):
        _status["ready"] = False
        return dict(_status)

    if _initialized:
        _status["ready"] = True
        return dict(_status)

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": name,
                "service.namespace": "openclaw-rmp",
            }
        )
        provider = TracerProvider(resource=resource)
        exporters_added = 0

        endpoint = (cfg.get("otlp_endpoint") or "").strip()
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )
            exporters_added += 1
            logger.info("OTLP trace export enabled: %s", endpoint)

        if cfg.get("console_export", False):
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
            exporters_added += 1
            logger.info("Console trace export enabled")

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("rmp", "1.0.0")
        _initialized = True
        _status["ready"] = True
        _status["error"] = None
        if exporters_added == 0:
            logger.info(
                "Telemetry enabled without exporters (spans recorded in-process only)"
            )
    except Exception as e:
        _status["ready"] = False
        _status["error"] = str(e)
        logger.warning("Telemetry init failed: %s", e)

    return dict(_status)


def get_tracer(name: str = "rmp"):
    global _tracer
    if _tracer is None:
        from opentelemetry import trace

        return trace.get_tracer(name, "1.0.0")
    return _tracer


def instrument_fastapi(app) -> bool:
    if not is_telemetry_enabled():
        return False
    if _status.get("fastapi_instrumented"):
        return True
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="/health")
        _status["fastapi_instrumented"] = True
        return True
    except Exception as e:
        logger.warning("FastAPI instrumentation failed: %s", e)
        return False


def get_temporal_client_kwargs() -> Dict[str, Any]:
    """Return kwargs for Temporal Client.connect when tracing is enabled."""
    if not is_telemetry_enabled() or not _status.get("ready"):
        return {}
    try:
        from temporalio.contrib.opentelemetry import TracingInterceptor

        _status["temporal_tracing"] = True
        return {"interceptors": [TracingInterceptor()]}
    except Exception as e:
        logger.warning("Temporal tracing interceptor unavailable: %s", e)
        return {}


def telemetry_status() -> Dict[str, Any]:
    return dict(_status)


@contextmanager
def trace_span(name: str, attributes: Optional[Dict[str, Any]] = None) -> Iterator[Any]:
    if not is_telemetry_enabled() or not _status.get("ready"):
        yield None
        return
    tracer = get_tracer("rmp")
    with tracer.start_as_current_span(name, attributes=attributes or {}) as span:
        yield span


def trace_activity(name: Optional[str] = None) -> Callable:
    """Decorator for Temporal activities — adds RMP attribute spans."""

    def decorator(fn: Callable) -> Callable:
        span_name = name or fn.__name__

        @wraps(fn)
        async def wrapper(payload: Dict[str, Any], *args, **kwargs):
            if not is_telemetry_enabled() or not _status.get("ready"):
                return await fn(payload, *args, **kwargs)
            attrs = _extract_attrs(payload)
            tracer = get_tracer("rmp.activities")
            with tracer.start_as_current_span(span_name, attributes=attrs) as span:
                try:
                    result = await fn(payload, *args, **kwargs)
                    if isinstance(result, dict):
                        for key in ("status", "quality", "is_valid"):
                            if key in result:
                                span.set_attribute(f"rmp.result.{key}", str(result[key])[:64])
                    return result
                except Exception as e:
                    span.record_exception(e)
                    span.set_attribute("rmp.error", str(e)[:200])
                    raise

        return wrapper

    return decorator


def traced_activity(name: Optional[str] = None) -> Callable:
    """Combine trace_activity + activity.defn for Temporal activities."""

    def decorator(fn: Callable):
        from temporalio import activity

        return activity.defn(trace_activity(name)(fn))

    return decorator
