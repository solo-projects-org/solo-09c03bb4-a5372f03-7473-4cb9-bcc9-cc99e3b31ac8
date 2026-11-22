"""Durable chord verdicts shared by the detail view and segment issuance."""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Database
from app.geometry import TrailGeometry
from app.main import app, set_database
from app.service import VerdictStore

from .conftest import make_trail_payload
from .test_properties import random_track, random_zones


def create(client, samples, **kwargs):
    resp = client.post("/trails", json=make_trail_payload(samples, **kwargs))
    assert resp.status_code == 201, resp.text
    return resp.json()


def issue(client, trail_id, progress, end, pins=None):
    return client.post(
        f"/trails/{trail_id}/segments",
        json={
            "observed_progress": progress,
            "end_index": end,
            "required_pins": pins or [],
        },
    )


def verdict_rows(db_path: Path) -> dict[tuple[int, int], bool]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT trail_id, a, b, valid FROM edge_verdicts"
        ).fetchall()
    return {(row[1], row[2]): bool(row[3]) for row in rows}


COLLINEAR = [(t, 3 * t, 2 * t) for t in range(6)]


def test_detail_and_issue_share_verdicts_and_keep_both_answers(client, db_path):
    # Registration returns the detail, which computes and stores the verdicts
    # needed for the whole-track minimum.
    detail = create(client, COLLINEAR)
    stored = verdict_rows(db_path)
    assert stored and all(valid for valid in stored.values())

    body = client.get(f"/trails/{detail['id']}").json()
    assert body["minimum_total_points"] == 2
    assert verdict_rows(db_path) == stored  # detail reused them

    # Issuance reuses the detail's verdicts: no new rows, same outcome.
    seg = issue(client, detail["id"], 0, 5)
    assert seg.status_code == 201, seg.text
    assert [p["index"] for p in seg.json()["points"]] == [0, 5]
    assert verdict_rows(db_path) == stored

    # A rejected (non-connectable) verdict is durable too.
    dip = [(0, 0, -20), (1, 0, 0), (2, 0, -20)]
    zone = {"x_min": -10, "x_max": 10, "y_min": -10, "y_max": 10}
    detail2 = create(client, dip, zones=[zone], error_limit=1000)
    client.get(f"/trails/{detail2['id']}")
    rows2 = verdict_rows(db_path)
    assert rows2[(0, 2)] is False
    seg2 = issue(client, detail2["id"], 0, 2)
    assert seg2.status_code == 201
    assert [p["index"] for p in seg2.json()["points"]] == [0, 1, 2]


def test_cached_verdicts_skip_dwell_component_reconstruction(client, monkeypatch):
    zone = {"x_min": -10, "x_max": 10, "y_min": -10, "y_max": 10}
    dip = [(0, 0, -20), (1, 0, 0), (2, 0, -20)]
    detail = create(client, dip, zones=[zone], error_limit=1000)

    # First read must build the original dwell components (nothing cached).
    client.get(f"/trails/{detail['id']}")

    def boom(self, rect):  # pragma: no cover - must never be reached
        raise AssertionError("dwell components rebuilt despite cached verdicts")

    monkeypatch.setattr(TrailGeometry, "_build_components", boom)
    # Detail and issuance are both answered from the durable verdicts.
    assert client.get(f"/trails/{detail['id']}").status_code == 200
    assert issue(client, detail["id"], 0, 2).status_code == 201


def test_verdicts_survive_restart_for_detail_and_issuance(db_path: Path, monkeypatch):
    samples = [(t, 3 * t, 2 * t) for t in range(6)]
    db = Database(str(db_path))
    set_database(db)
    with TestClient(app) as first:
        detail = create(first, samples)
        assert first.get(f"/trails/{detail['id']}").status_code == 200
    set_database(None)  # type: ignore[arg-type]

    set_database(Database(str(db_path)))
    with TestClient(app) as second:
        def boom(self, rect):  # pragma: no cover - must never be reached
            raise AssertionError("recomputed verdict after restart")

        monkeypatch.setattr(TrailGeometry, "_build_components", boom)
        body = second.get(f"/trails/{detail['id']}").json()
        assert body["minimum_total_points"] == 2
        seg = issue(second, detail["id"], 0, 5)
        assert seg.status_code == 201
        assert [p["index"] for p in seg.json()["points"]] == [0, 5]
    set_database(None)  # type: ignore[arg-type]


def test_over_budget_rejection_keeps_verdicts_but_not_progress(client, db_path):
    samples = [(0, 0, 0), (1, 10, 1), (2, 20, 0), (3, 30, 1), (4, 40, 0)]
    detail = create(client, samples, error_limit=0, point_limit=4)

    resp = issue(client, detail["id"], 0, 4)
    assert resp.status_code == 409
    assert resp.json()["detail"]["minimum_total_points"] == 5
    assert verdict_rows(db_path)  # computed verdicts were kept
    assert client.get(f"/trails/{detail['id']}").json()["progress_index"] == 0
    assert client.get(f"/trails/{detail['id']}/segments").json() == []


def test_old_database_is_backfilled_on_demand(db_path: Path, monkeypatch):
    samples = [(t, 3 * t, 2 * t) for t in range(6)]
    db = Database(str(db_path))
    set_database(db)
    with TestClient(app) as first:
        detail = create(first, samples)
        client_detail = first.get(f"/trails/{detail['id']}").json()
    set_database(None)  # type: ignore[arg-type]

    # Simulate a database written before verdicts existed.
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE edge_verdicts")

    set_database(Database(str(db_path)))  # startup re-creates the table
    with TestClient(app) as legacy:
        assert verdict_rows(db_path) == {}
        # Listing all trails also backfills lazily, before any single detail.
        listed = legacy.get("/trails").json()
        assert listed[0]["minimum_total_points"] == client_detail["minimum_total_points"]
        assert verdict_rows(db_path)  # backfilled by the list read
        # Dropping again proves the detail path backfills on its own too.
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM edge_verdicts")
        body = legacy.get(f"/trails/{detail['id']}").json()
        assert body["minimum_total_points"] == client_detail["minimum_total_points"]
        assert verdict_rows(db_path)
        # Old issued segments are still served unchanged.
        assert legacy.get(f"/trails/{detail['id']}/segments").json() == []
    set_database(None)  # type: ignore[arg-type]


def test_resume_after_restart_reuses_verdicts_and_counts_seam_once(
    client, db_path: Path
):
    samples = [(t, 3 * t, 2 * t) for t in range(11)]
    detail = create(client, samples, point_limit=3)
    first = issue(client, detail["id"], 0, 5)
    assert first.status_code == 201

    # A stale duplicate does not advance progress.
    dup = issue(client, detail["id"], 0, 5)
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "stale_progress"
    assert len(client.get(f"/trails/{detail['id']}/segments").json()) == 1

    set_database(None)  # type: ignore[arg-type]
    set_database(Database(str(db_path)))
    with TestClient(app) as resumed:
        body = resumed.get(f"/trails/{detail['id']}/segments").json()
        assert len(body) == 1
        second = issue(resumed, detail["id"], 5, 10)
        assert second.status_code == 201
        assert second.json()["start_index"] == 5
        assert second.json()["total_points_used"] == 3  # 0, 5, 10
        assert resumed.get(f"/trails/{detail['id']}").json()["progress_index"] == 10
    set_database(None)  # type: ignore[arg-type]


def test_detail_minimum_uses_current_seam_and_points_already_used(client):
    # Force a detour: after a short first segment the remainder needs 3 more
    # points, so the detail must report used + remaining - 1.
    samples = [(0, 0, 0), (1, 0, 10), (2, 0, 0), (3, 0, 10), (4, 0, 0)]
    detail = create(client, samples, error_limit=0, point_limit=10)
    assert client.get(f"/trails/{detail['id']}").json()["minimum_total_points"] == 5

    # First segment keeps only its endpoints (adjacent chord is exact).
    seg = issue(client, detail["id"], 0, 1)
    assert seg.status_code == 201
    body = client.get(f"/trails/{detail['id']}").json()
    assert body["issued_point_count"] == 2
    assert body["minimum_total_points"] == 5  # 0,1 plus samples 2,3,4
    assert body["remaining_point_budget"] == 8


def _geom(samples, zones=(), limit=0, verdicts=None):
    from fractions import Fraction

    return TrailGeometry(
        [s[0] for s in samples],
        [s[1] for s in samples],
        [s[2] for s in samples],
        list(zones),
        Fraction(limit),
        verdicts=verdicts,
    )


def test_geometry_consults_store_before_computing(monkeypatch):
    # A dip through a zone: 0->2 is genuinely not connectable.
    zone = (-5, 5, -5, 5)
    dip = [(0, 0, -10), (1, 0, 0), (2, 0, -10)]

    class Store:
        def __init__(self):
            self.reads = []
            self.writes = []

        def get(self, a, b):
            self.reads.append((a, b))
            return True  # claim the chord is fine

        def put(self, a, b, valid):
            self.writes.append((a, b, valid))

    store = Store()
    g = _geom(dip, zones=[zone], limit=0, verdicts=store)
    assert g.edge_valid(0, 2) is True  # stored verdict wins
    assert store.reads == [(0, 2)]
    assert store.writes == []  # nothing recomputed, nothing re-stored


def test_geometry_stores_both_true_and_false(monkeypatch):
    zone = (-5, 5, -5, 5)
    dip = [(0, 0, -10), (1, 0, 0), (2, 0, -10)]

    class Store:
        def __init__(self):
            self.data = {}

        def get(self, a, b):
            return self.data.get((a, b))

        def put(self, a, b, valid):
            self.data[(a, b)] = valid

    store = Store()
    g = _geom(dip, zones=[zone], limit=0, verdicts=store)
    assert g.edge_valid(0, 2) is False
    assert g.edge_valid(1, 2) is True  # adjacent: trivially valid, not stored
    assert store.data == {(0, 2): False}

    # A second geometry sharing the same store never recomputes.
    g2 = _geom(dip, zones=[zone], limit=0, verdicts=store)

    def boom(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("recomputed despite stored verdict")

    monkeypatch.setattr(TrailGeometry, "_error_ok", boom)
    monkeypatch.setattr(TrailGeometry, "_build_components", boom)
    assert g2.edge_valid(0, 2) is False


def test_verdict_store_buffers_only_new_answers():
    store = VerdictStore("t", {(0, 2): False})
    assert store.get(0, 2) is False
    store.put(0, 2, True)  # already known: ignored, no pending write
    assert store.pending == {}
    store.put(0, 3, True)
    store.put(1, 3, False)
    assert store.pending == {(0, 3): True, (1, 3): False}
    assert store.get(0, 3) is True


def test_verdict_flush_failure_is_swallowed():
    import sqlite3
    from contextlib import contextmanager

    from app.service import Service

    class BrokenDB:
        @contextmanager
        def write_transaction(self):
            raise sqlite3.OperationalError("disk full")
            yield  # pragma: no cover

    # A cache write error must never turn into a request error.
    Service(BrokenDB())._flush_verdicts("t", {(0, 2): True})
    Service(BrokenDB())._flush_verdicts("t", {})  # nothing to do, no write


def test_overflow_retry_with_new_end_reuses_earlier_verdicts(client, db_path):
    # The first attempt fails on budget; retrying with a different end and a
    # review pin must consult the stored verdicts instead of recomputing them.
    samples = [(0, 0, 0), (1, 10, 1), (2, 20, 0), (3, 30, 1), (4, 40, 0)]
    detail = create(client, samples, error_limit=0, point_limit=4)
    assert issue(client, detail["id"], 0, 4).status_code == 409
    stored = verdict_rows(db_path)
    assert stored

    resp = issue(client, detail["id"], 0, 2, pins=[1])
    assert resp.status_code == 409
    assert verdict_rows(db_path) == stored  # no new computation needed
    assert client.get(f"/trails/{detail['id']}").json()["issued_point_count"] == 0


def test_detail_and_issuance_agree_on_minimum_after_partial_handover(client, db_path):
    # After a partial handover the detail's minimum (current seam + points
    # already used) must equal what completing the trail actually costs.
    samples = [(0, 0, 0), (1, 0, 10), (2, 0, 0), (3, 0, 10), (4, 0, 0)]
    detail = create(client, samples, error_limit=0, point_limit=5)
    assert issue(client, detail["id"], 0, 1).status_code == 201
    body = client.get(f"/trails/{detail['id']}").json()
    assert body["issued_point_count"] == 2
    assert body["minimum_total_points"] == 5

    resp = issue(client, detail["id"], 1, 4)
    assert resp.status_code == 201
    assert resp.json()["total_points_used"] == body["minimum_total_points"]
    assert client.get(f"/trails/{detail['id']}").json()["progress_index"] == 4


def test_verdict_cache_does_not_change_results(client):
    # Same random handovers, with a cold cache and after a restart: identical
    # point lists prove caching is purely an optimisation.
    rng = random.Random(1234)
    samples = random_track(rng, 24)
    zones = random_zones(rng, 3)
    zone_payload = [
        {"x_min": z[0], "x_max": z[1], "y_min": z[2], "y_max": z[3]}
        for z in zones
    ]
    detail = create(client, samples, zones=zone_payload, error_limit="2.5",
                    point_limit=200)
    first = issue(client, detail["id"], 0, 12, pins=[7]).json()
    second = issue(client, detail["id"], 12, 23, pins=[18]).json()

    # Replay the same requests on a brand-new trail (fresh verdict table).
    detail2 = create(client, samples, zones=zone_payload, error_limit="2.5",
                     point_limit=200)
    first2 = issue(client, detail2["id"], 0, 12, pins=[7]).json()
    second2 = issue(client, detail2["id"], 12, 23, pins=[18]).json()
    assert [p["index"] for p in first2["points"]] == [p["index"] for p in first["points"]]
    assert [p["index"] for p in second2["points"]] == [
        p["index"] for p in second["points"]
    ]


def test_retry_with_different_end_succeeds_after_overflow(client, db_path):
    # End 1 forces a detour that costs 4 points in total; end 2 costs 3, which
    # is the whole budget.  The reviewer's first choice overflows, the retry
    # with a different end succeeds and only then advances progress.
    samples = [(0, 0, 0), (1, 0, 10), (2, 0, 20), (3, 0, 10), (4, 0, 0)]
    detail = create(client, samples, error_limit=5, point_limit=3)

    rejected = issue(client, detail["id"], 0, 1)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["minimum_total_points"] == 4
    assert client.get(f"/trails/{detail['id']}").json()["issued_point_count"] == 0

    accepted = issue(client, detail["id"], 0, 2)
    assert accepted.status_code == 201
    assert [p["index"] for p in accepted.json()["points"]] == [0, 2]
    assert accepted.json()["total_points_used"] == 2

    tail = issue(client, detail["id"], 2, 4)
    assert tail.status_code == 201
    assert [p["index"] for p in tail.json()["points"]] == [2, 4]
    assert tail.json()["total_points_used"] == 3
    assert client.get(f"/trails/{detail['id']}").json()["progress_index"] == 4


def test_retry_with_different_pin_succeeds_after_overflow(client, db_path):
    # Keeping review pin 2 costs a third point, which the limit of 2 forbids.
    # Dropping it lets the same span collapse to its endpoints, so the retry
    # succeeds; the rejected attempt's verdicts are reused, not recomputed.
    samples = [(t, 3 * t, 2 * t) for t in range(5)]
    detail = create(client, samples, error_limit=0, point_limit=2)

    rejected = issue(client, detail["id"], 0, 4, pins=[2])
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["minimum_total_points"] == 3
    stored = verdict_rows(db_path)
    assert stored

    accepted = issue(client, detail["id"], 0, 4)
    assert accepted.status_code == 201
    assert [p["index"] for p in accepted.json()["points"]] == [0, 4]
    assert accepted.json()["total_points_used"] == 2
    assert verdict_rows(db_path) == stored
