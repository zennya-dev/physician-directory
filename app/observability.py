from prometheus_client import Counter, Histogram


HTTP_REQUESTS = Counter(
    "medical_service_http_requests_total",
    "HTTP requests handled by medical-service",
    ["method", "route", "status"],
)

HTTP_LATENCY = Histogram(
    "medical_service_http_request_seconds",
    "HTTP request latency in seconds",
    ["method", "route", "status"],
)


def record_http_request(method: str, route: str, status: int, duration_seconds: float) -> None:
    status_label = str(status)
    HTTP_REQUESTS.labels(method=method, route=route, status=status_label).inc()
    HTTP_LATENCY.labels(method=method, route=route, status=status_label).observe(duration_seconds)
