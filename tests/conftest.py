"""Shared test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Database
from app.main import app, set_database


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "handover.db"


@pytest.fixture()
def client(db_path: Path):
    set_database(Database(str(db_path)))
    with TestClient(app) as test_client:
        yield test_client
    set_database(None)  # type: ignore[arg-type]


def make_trail_payload(
    samples: list[tuple[int, int, int]],
    zones: list[dict] | None = None,
    error_limit: float | str = 0,
    point_limit: int = 200,
    name: str = "test-trail",
) -> dict:
    return {
        "name": name,
        "samples": [{"t": t, "x": x, "y": y} for t, x, y in samples],
        "zones": zones or [],
        "error_limit": error_limit,
        "point_limit": point_limit,
    }
