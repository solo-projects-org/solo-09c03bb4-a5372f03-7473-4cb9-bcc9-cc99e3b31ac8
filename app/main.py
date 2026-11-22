"""FastAPI application: wetlands track handover."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .db import DEFAULT_DB_PATH, Database
from .schemas import (
    ErrorResponse,
    Health,
    SegmentOut,
    SegmentRequest,
    TrailCreate,
    TrailDetail,
    TrailSummary,
)
from .service import ApiError, Service

app = FastAPI(
    title="湿地轨迹分段交接 API",
    version="1.0.0",
    description=(
        "把湿地巡护轨迹按时间切成若干段交给 GIS。已签发段不可变；"
        "每段只选原采样点，首尾必留，接缝只计一次，简化误差与驻留时段均有保证。"
    ),
)

_database: Database | None = None


def get_service() -> Service:
    global _database
    if _database is None:
        _database = Database(os.environ.get("HANDOVER_DB", DEFAULT_DB_PATH))
    return Service(_database)


ServiceDep = Annotated[Service, Depends(get_service)]


def set_database(db: Database | None) -> None:
    """Test hook: point the app at a temporary database (``None`` resets)."""
    global _database
    _database = db


@app.exception_handler(ApiError)
async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"detail": {"code": exc.code, "message": exc.message, **exc.extra}},
    )


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    problems = [
        {
            "loc": [str(part) for part in err.get("loc", ())],
            "msg": err.get("msg", ""),
        }
        for err in exc.errors()
    ]
    summary = "; ".join(
        f"{'.'.join(item['loc'])}: {item['msg']}" for item in problems
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "validation_error",
                "message": f"请求内容不合法：{summary}",
                "problems": problems,
            }
        },
    )


@app.get("/healthz", response_model=Health, tags=["meta"])
def healthz() -> Health:
    return Health(status="ok")


@app.post(
    "/trails",
    response_model=TrailDetail,
    status_code=201,
    tags=["trails"],
    responses={422: {"model": ErrorResponse}},
    summary="登记轨迹",
)
def create_trail(payload: TrailCreate, service: ServiceDep):
    return service.create_trail(payload)


@app.get("/trails", response_model=list[TrailSummary], tags=["trails"])
def list_trails(service: ServiceDep):
    return service.list_trails()


@app.get(
    "/trails/{trail_id}",
    response_model=TrailDetail,
    tags=["trails"],
    responses={404: {"model": ErrorResponse}},
)
def get_trail(trail_id: str, service: ServiceDep):
    return service.get_detail(trail_id)


@app.get(
    "/trails/{trail_id}/segments",
    response_model=list[SegmentOut],
    tags=["segments"],
    summary="查已签发段（重启后续交）",
    responses={404: {"model": ErrorResponse}},
)
def list_segments(trail_id: str, service: ServiceDep):
    return service.list_segments(trail_id)


@app.get(
    "/trails/{trail_id}/segments/{seq}",
    response_model=SegmentOut,
    tags=["segments"],
    responses={404: {"model": ErrorResponse}},
)
def get_segment(trail_id: str, seq: int, service: ServiceDep):
    return service.get_segment(trail_id, seq)


@app.post(
    "/trails/{trail_id}/segments",
    response_model=SegmentOut,
    status_code=201,
    tags=["segments"],
    summary="申请下一段",
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "进度过期 / 点数超额 / 已完成"},
        422: {"model": ErrorResponse, "description": "终点或复核点非法"},
    },
)
def request_segment(
    trail_id: str,
    payload: SegmentRequest,
    service: ServiceDep,
):
    return service.request_segment(
        trail_id,
        observed_progress=payload.observed_progress,
        end_index=payload.end_index,
        required_pins=list(payload.required_pins),
    )
