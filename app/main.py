import logging
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import get_settings
from app.database import ensure_database_schema
from app.observability import record_http_request
from app.routers import routers_physicians


logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_database_schema()
    yield


app = FastAPI(
    title="Physician Directory",
    root_path=settings.root_path,
    root_path_in_servers=False,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.include_router(routers_physicians.router)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


@app.middleware("http")
async def observe_http_requests(request: Request, call_next):
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        record_http_request(request.method, _route_template(request), 500, perf_counter() - started)
        raise

    record_http_request(request.method, _route_template(request), response.status_code, perf_counter() - started)
    return response


@app.get("/")
async def root():
    return {"message": "Welcome to the Physician Directory"}


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": settings.service_name}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
