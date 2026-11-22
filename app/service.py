"""Business rules for issuing the next segment."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from fractions import Fraction

from .db import Database, encode, new_trail_id, utcnow
from .geometry import TrailGeometry, chain_via_pins
from .schemas import (
    Sample,
    SampleOut,
    SegmentOut,
    TrailCreate,
    TrailDetail,
    TrailSummary,
    Zone,
    ZoneOut,
)

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """Raised for a client-visible failure; mapped to JSON in main.py."""

    def __init__(self, status: int, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = {k: v for k, v in extra.items() if v is not None}


@dataclass
class TrailRecord:
    id: str
    name: str | None
    created_at: str
    error_limit: str
    point_limit: int
    samples: list[Sample]
    zones: list[Zone]

    @property
    def n(self) -> int:
        return len(self.samples)

    def geometry(self, verdicts: "VerdictStore | None" = None) -> TrailGeometry:
        return TrailGeometry(
            times=[s.t for s in self.samples],
            xs=[s.x for s in self.samples],
            ys=[s.y for s in self.samples],
            rects=[
                (z.x_min, z.x_max, z.y_min, z.y_max) for z in self.zones
            ],
            error_limit=Fraction(self.error_limit),
            verdicts=verdicts,
        )


class VerdictStore:
    """Per-trail chord verdicts: the in-memory view plus this run's new ones.

    Reads come from the durable set loaded at request start; ``put`` records a
    freshly computed verdict and buffers it so it can be flushed even when the
    surrounding issuance transaction is rolled back (point-limit overflow).
    """

    def __init__(
        self, trail_id: str, known: dict[tuple[int, int], bool] | None = None
    ) -> None:
        self.trail_id = trail_id
        self._known: dict[tuple[int, int], bool] = dict(known or {})
        self.pending: dict[tuple[int, int], bool] = {}

    def get(self, a: int, b: int) -> bool | None:
        return self._known.get((a, b))

    def put(self, a: int, b: int, valid: bool) -> None:
        if (a, b) not in self._known:
            self._known[(a, b)] = valid
            self.pending[(a, b)] = valid


def _row_to_trail(row: sqlite3.Row) -> TrailRecord:
    samples = [Sample(**item) for item in json.loads(row["samples"])]
    zones = [Zone(**item) for item in json.loads(row["zones"])]
    return TrailRecord(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        error_limit=row["error_limit"],
        point_limit=row["point_limit"],
        samples=samples,
        zones=zones,
    )


def _row_to_segment(row: sqlite3.Row) -> SegmentOut:
    points = [
        SampleOut(index=item["index"], t=item["t"], x=item["x"], y=item["y"])
        for item in json.loads(row["points"])
    ]
    used = row["total_points_used"]
    return SegmentOut(
        trail_id=row["trail_id"],
        seq=row["seq"],
        start_index=row["start_index"],
        end_index=row["end_index"],
        observed_progress=row["observed_progress"],
        required_pins=json.loads(row["required_pins"]),
        points=points,
        point_count=row["point_count"],
        new_point_count=row["new_point_count"],
        total_points_used=used,
        point_limit=row["point_limit"],
        remaining_point_budget=max(0, row["point_limit"] - used),
        remaining_minimum_points=row["remaining_minimum_points"],
        remaining_new_points=row["remaining_new_points"],
        minimum_total_points=row["minimum_total_points"],
        created_at=row["created_at"],
    )


class Service:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def _load_trail(self, conn: sqlite3.Connection, trail_id: str) -> TrailRecord:
        row = conn.execute("SELECT * FROM trails WHERE id = ?", (trail_id,)).fetchone()
        if row is None:
            raise ApiError(404, "trail_not_found", f"轨迹 {trail_id} 不存在")
        return _row_to_trail(row)

    @staticmethod
    def _segments(conn: sqlite3.Connection, trail_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM segments WHERE trail_id = ? ORDER BY seq", (trail_id,)
        ).fetchall()

    @staticmethod
    def _progress(rows: list[sqlite3.Row]) -> int:
        return rows[-1]["end_index"] if rows else 0

    @staticmethod
    def _used(rows: list[sqlite3.Row]) -> int:
        return rows[-1]["total_points_used"] if rows else 0

    @staticmethod
    def _load_all_verdicts(
        conn: sqlite3.Connection,
    ) -> dict[str, dict[tuple[int, int], bool]]:
        rows = conn.execute(
            "SELECT trail_id, a, b, valid FROM edge_verdicts"
        ).fetchall()
        grouped: dict[str, dict[tuple[int, int], bool]] = {}
        for row in rows:
            grouped.setdefault(row["trail_id"], {})[(row["a"], row["b"])] = bool(
                row["valid"]
            )
        return grouped

    @staticmethod
    def _load_verdicts(
        conn: sqlite3.Connection, trail_id: str
    ) -> dict[tuple[int, int], bool]:
        rows = conn.execute(
            "SELECT a, b, valid FROM edge_verdicts WHERE trail_id = ?", (trail_id,)
        ).fetchall()
        return {(row["a"], row["b"]): bool(row["valid"]) for row in rows}

    def _flush_verdicts(
        self, trail_id: str, verdicts: dict[tuple[int, int], bool]
    ) -> None:
        """Persist freshly computed verdicts in their own transaction.

        Called after the issuance transaction has committed or rolled back, so
        an over-budget rejection still leaves this run's verdicts reusable and
        the write lock is never taken twice at once.

        Caching is an optimisation: a failed flush only costs a later
        recomputation, so it never turns an issued segment into an error.
        """
        if not verdicts:
            return
        try:
            with self.db.write_transaction() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO edge_verdicts (trail_id, a, b, valid)"
                    " VALUES (?, ?, ?, ?)",
                    [
                        (trail_id, a, b, 1 if valid else 0)
                        for (a, b), valid in verdicts.items()
                    ],
                )
        except sqlite3.Error:
            logger.warning(
                "could not persist %d edge verdicts for trail %s",
                len(verdicts),
                trail_id,
                exc_info=True,
            )

    def _summary(
        self,
        trail: TrailRecord,
        rows: list[sqlite3.Row],
        verdicts: VerdictStore | None = None,
    ) -> TrailSummary:
        geom = trail.geometry(verdicts)
        progress = self._progress(rows)
        used = self._used(rows)
        remaining_min = geom.minimum_points(progress, trail.n - 1)
        assert remaining_min is not None  # adjacent edges are always valid
        # ``remaining_min`` counts the progress point once; ``used`` already
        # includes it when a segment has been issued.
        minimum_total = remaining_min if not rows else used + remaining_min - 1
        return TrailSummary(
            id=trail.id,
            name=trail.name,
            created_at=trail.created_at,
            error_limit=trail.error_limit,
            point_limit=trail.point_limit,
            sample_count=trail.n,
            zone_count=len(trail.zones),
            progress_index=progress,
            issued_segment_count=len(rows),
            issued_point_count=used,
            remaining_point_budget=max(0, trail.point_limit - used),
            minimum_total_points=minimum_total,
            can_complete_within_limit=minimum_total <= trail.point_limit,
        )

    def list_trails(self) -> list[TrailSummary]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trails ORDER BY created_at, id"
            ).fetchall()
            known = self._load_all_verdicts(conn)
            summaries = []
            pending: list[tuple[str, dict[tuple[int, int], bool]]] = []
            for row in rows:
                trail = _row_to_trail(row)
                verdicts = VerdictStore(trail.id, known.get(trail.id))
                summaries.append(
                    self._summary(trail, self._segments(conn, trail.id), verdicts)
                )
                pending.append((trail.id, verdicts.pending))
        for trail_id, new_verdicts in pending:
            self._flush_verdicts(trail_id, new_verdicts)
        return summaries

    def get_detail(self, trail_id: str) -> TrailDetail:
        with self.db.connect() as conn:
            trail = self._load_trail(conn, trail_id)
            rows = self._segments(conn, trail_id)
            verdicts = VerdictStore(
                trail_id, self._load_verdicts(conn, trail_id)
            )
            summary = self._summary(trail, rows, verdicts)
        # Backfill any verdicts this read had to compute (old databases).
        self._flush_verdicts(trail_id, verdicts.pending)
        return TrailDetail(
            **summary.model_dump(),
            samples=[
                SampleOut(index=i, **s.model_dump()) for i, s in enumerate(trail.samples)
            ],
            zones=[ZoneOut(index=i, **z.model_dump()) for i, z in enumerate(trail.zones)],
            segments=[_row_to_segment(r) for r in rows],
        )

    def list_segments(self, trail_id: str) -> list[SegmentOut]:
        with self.db.connect() as conn:
            self._load_trail(conn, trail_id)
            return [_row_to_segment(r) for r in self._segments(conn, trail_id)]

    def get_segment(self, trail_id: str, seq: int) -> SegmentOut:
        with self.db.connect() as conn:
            self._load_trail(conn, trail_id)
            row = conn.execute(
                "SELECT * FROM segments WHERE trail_id = ? AND seq = ?",
                (trail_id, seq),
            ).fetchone()
            if row is None:
                raise ApiError(
                    404, "segment_not_found", f"轨迹 {trail_id} 没有第 {seq} 段"
                )
            return _row_to_segment(row)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def create_trail(self, payload: TrailCreate) -> TrailDetail:
        trail_id = new_trail_id()
        created = utcnow()
        with self.db.write_transaction() as conn:
            conn.execute(
                "INSERT INTO trails (id, name, created_at, error_limit, point_limit,"
                " samples, zones) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    trail_id,
                    payload.name,
                    created,
                    format(payload.error_limit, "f"),
                    payload.point_limit,
                    encode([s.model_dump() for s in payload.samples]),
                    encode([z.model_dump() for z in payload.zones]),
                ),
            )
        return self.get_detail(trail_id)

    def request_segment(
        self,
        trail_id: str,
        observed_progress: int,
        end_index: int,
        required_pins: list[int],
    ) -> SegmentOut:
        verdicts: VerdictStore | None = None
        try:
            with self.db.write_transaction() as conn:
                trail = self._load_trail(conn, trail_id)
                rows = self._segments(conn, trail_id)
                progress = self._progress(rows)
                used = self._used(rows)

                if observed_progress != progress:
                    raise ApiError(
                        409,
                        "stale_progress",
                        f"所见进度 {observed_progress} 已过期，当前进度为 {progress}；"
                        "请重新拉取已签发段后再提交。",
                        trail_id=trail_id,
                        current_progress=progress,
                    )
                if progress >= trail.n - 1:
                    raise ApiError(
                        409,
                        "trail_complete",
                        "整条轨迹已全部签发，没有下一段。",
                        trail_id=trail_id,
                        current_progress=progress,
                    )
                if not (progress < end_index <= trail.n - 1):
                    raise ApiError(
                        422,
                        "invalid_end_index",
                        f"终点必须满足 {progress} < end_index <= {trail.n - 1}",
                        trail_id=trail_id,
                        current_progress=progress,
                    )

                pins = sorted(set(required_pins))
                outside = [p for p in pins if not (progress <= p <= end_index)]
                if outside:
                    raise ApiError(
                        422,
                        "pin_outside_segment",
                        f"复核点 {outside} 不在本段 [{progress}, {end_index}] 内",
                        trail_id=trail_id,
                        current_progress=progress,
                    )

                # Only the chords this request actually needs are consulted or
                # computed: the current segment (through the pins), then the
                # remainder from this end to the trail end.  Both the detail
                # view and this API share the durable verdict set.
                verdicts = VerdictStore(
                    trail_id, self._load_verdicts(conn, trail_id)
                )
                geom = trail.geometry(verdicts)
                chain = chain_via_pins(geom, progress, end_index, pins)

                remaining_min = geom.minimum_points(end_index, trail.n - 1)
                assert remaining_min is not None
                segment_min = len(chain)
                new_points = segment_min if not rows else segment_min - 1
                total_after = used + new_points
                remaining_new = remaining_min - 1
                minimum_total = total_after + remaining_new

                if minimum_total > trail.point_limit:
                    raise ApiError(
                        409,
                        "point_limit_exceeded",
                        "按本次选择完成整条轨迹至少需要 "
                        f"{minimum_total} 个点，超过全程点数上限 {trail.point_limit}；"
                        "进度未改变。",
                        trail_id=trail_id,
                        current_progress=progress,
                        point_limit=trail.point_limit,
                        issued_point_count=used,
                        segment_minimum_points=segment_min,
                        remaining_minimum_points=remaining_min,
                        minimum_total_points=minimum_total,
                    )

                seq = len(rows) + 1
                created = utcnow()
                point_payload = [
                    {
                        "index": i,
                        "t": trail.samples[i].t,
                        "x": trail.samples[i].x,
                        "y": trail.samples[i].y,
                    }
                    for i in chain
                ]
                conn.execute(
                    "INSERT INTO segments (trail_id, seq, start_index, end_index,"
                    " observed_progress, required_pins, points, point_count,"
                    " new_point_count, total_points_used, remaining_minimum_points,"
                    " remaining_new_points, minimum_total_points, point_limit,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        trail_id,
                        seq,
                        progress,
                        end_index,
                        observed_progress,
                        encode(pins),
                        encode(point_payload),
                        segment_min,
                        new_points,
                        total_after,
                        remaining_min,
                        remaining_new,
                        minimum_total,
                        trail.point_limit,
                        created,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM segments WHERE trail_id = ? AND seq = ?",
                    (trail_id, seq),
                ).fetchone()
                result = _row_to_segment(row)
        finally:
            # Verdicts computed during this attempt survive both a committed
            # issuance and an over-budget rejection; they are never computed
            # again, in this process or after a restart.
            if verdicts is not None:
                self._flush_verdicts(trail_id, verdicts.pending)
        return result
