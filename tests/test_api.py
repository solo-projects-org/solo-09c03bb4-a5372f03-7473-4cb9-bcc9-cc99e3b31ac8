"""API-level tests: issuance rules, immutability, stale progress, restart."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Database
from app.main import app, set_database

from .conftest import make_trail_payload

STRAIGHT = [(t, 3 * t, 2 * t) for t in range(11)]  # collinear, 11 samples


def create(client, samples=STRAIGHT, **kwargs):
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


def test_create_and_validation_bounds(client):
    detail = create(client)
    assert detail["sample_count"] == 11
    assert detail["progress_index"] == 0
    assert detail["issued_segment_count"] == 0

    bad = make_trail_payload([(t, 0, 0) for t in range(201)])
    assert client.post("/trails", json=bad).status_code == 422

    dup_time = make_trail_payload([(0, 0, 0), (0, 1, 1)])
    assert client.post("/trails", json=dup_time).status_code == 422

    float_time = make_trail_payload([(0, 0, 0), (1, 1, 1)])
    float_time["samples"][1]["t"] = 1.5
    assert client.post("/trails", json=float_time).status_code == 422

    bad_zone = make_trail_payload(STRAIGHT, zones=[{"x_min": 5, "x_max": 1, "y_min": 0, "y_max": 3}])
    assert client.post("/trails", json=bad_zone).status_code == 422

    negative_limit = make_trail_payload(STRAIGHT, error_limit=-1)
    assert client.post("/trails", json=negative_limit).status_code == 422


def test_straight_track_issues_single_minimal_segment(client):
    detail = create(client, point_limit=2)
    seg = issue(client, detail["id"], 0, 10)
    assert seg.status_code == 201, seg.text
    body = seg.json()
    assert [p["index"] for p in body["points"]] == [0, 10]
    assert body["point_count"] == 2
    assert body["total_points_used"] == 2
    assert body["remaining_minimum_points"] == 1
    assert body["remaining_new_points"] == 0
    assert body["minimum_total_points"] == 2

    again = issue(client, detail["id"], 10, 10)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "trail_complete"


def test_seams_counted_once_across_segments(client):
    detail = create(client, point_limit=4)
    first = issue(client, detail["id"], 0, 5).json()
    assert first["total_points_used"] == 2
    second = issue(client, detail["id"], 5, 10).json()
    assert second["start_index"] == 5
    assert second["points"][0]["index"] == 5
    assert second["total_points_used"] == 3  # 0,5,10
    assert second["remaining_new_points"] == 0

    listed = client.get(f"/trails/{detail['id']}/segments").json()
    assert [(s["seq"], s["start_index"], s["end_index"]) for s in listed] == [
        (1, 0, 5),
        (2, 5, 10),
    ]


def test_stale_progress_is_rejected(client):
    detail = create(client)
    assert issue(client, detail["id"], 0, 8).status_code == 201
    stale = issue(client, detail["id"], 0, 10)
    assert stale.status_code == 409
    detail_body = stale.json()["detail"]
    assert detail_body["code"] == "stale_progress"
    assert detail_body["current_progress"] == 8
    # Nothing was issued for the stale attempt.
    assert len(client.get(f"/trails/{detail['id']}/segments").json()) == 1
    # Retrying with the fresh progress works.
    assert issue(client, detail["id"], 8, 10).status_code == 201


def test_required_pins_are_kept_and_must_be_inside(client):
    detail = create(client)
    seg = issue(client, detail["id"], 0, 6, pins=[2, 4])
    assert seg.status_code == 201, seg.text
    indexes = [p["index"] for p in seg.json()["points"]]
    assert 2 in indexes and 4 in indexes
    assert indexes[0] == 0 and indexes[-1] == 6

    outside = issue(client, detail["id"], 6, 10, pins=[3])
    assert outside.status_code == 422
    assert outside.json()["detail"]["code"] == "pin_outside_segment"


def test_zero_error_limit_keeps_every_non_collinear_sample(client):
    wiggle = [(0, 0, 0), (1, 0, 4), (2, 0, 0)]
    detail = create(client, samples=wiggle, error_limit=0, point_limit=10)
    seg = issue(client, detail["id"], 0, 2).json()
    assert [p["index"] for p in seg["points"]] == [0, 1, 2]


def test_dwell_zones_force_retention(client):
    zone = {"x_min": -10, "x_max": 10, "y_min": -10, "y_max": 10}
    dip = [(0, 0, -20), (1, 0, 0), (2, 0, -20)]
    detail = create(client, samples=dip, zones=[zone], error_limit=100, point_limit=10)
    seg = issue(client, detail["id"], 0, 2).json()
    assert [p["index"] for p in seg["points"]] == [0, 1, 2]


def test_point_limit_exceeded_reports_minimum_and_keeps_progress(client):
    # 5 samples with error 0 and non-collinear geometry -> needs all 5.
    samples = [(0, 0, 0), (1, 10, 1), (2, 20, 0), (3, 30, 1), (4, 40, 0)]
    detail = create(client, samples=samples, error_limit=0, point_limit=4)
    resp = issue(client, detail["id"], 0, 4)
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["code"] == "point_limit_exceeded"
    assert body["minimum_total_points"] == 5
    assert body["point_limit"] == 4
    assert client.get(f"/trails/{detail['id']}/segments").json() == []
    assert client.get(f"/trails/{detail['id']}").json()["progress_index"] == 0


def test_partial_segment_rejected_when_remainder_will_not_fit(client):
    # Track needs 5 points in total; limit 4. A first segment that leaves no
    # room for the remainder must be refused even though it is small.
    samples = [(0, 0, 0), (1, 10, 1), (2, 20, 0), (3, 30, 1), (4, 40, 0)]
    detail = create(client, samples=samples, error_limit=0, point_limit=4)
    resp = issue(client, detail["id"], 0, 2)
    assert resp.status_code == 409
    detail_body = resp.json()["detail"]
    assert detail_body["minimum_total_points"] == 5
    assert detail_body["remaining_minimum_points"] == 3
    assert client.get(f"/trails/{detail['id']}/segments").json() == []


def test_issued_segments_are_immutable(client):
    detail = create(client)
    seg = issue(client, detail["id"], 0, 5).json()
    actions = [
        client.put(f"/trails/{detail['id']}/segments/{seg['seq']}", json={}),
        client.patch(f"/trails/{detail['id']}/segments/{seg['seq']}", json={}),
        client.delete(f"/trails/{detail['id']}/segments/{seg['seq']}"),
        client.delete(f"/trails/{detail['id']}"),
    ]
    assert all(r.status_code == 405 for r in actions)
    after = client.get(f"/trails/{detail['id']}/segments/{seg['seq']}").json()
    assert after == seg


def test_restart_keeps_segments_and_allows_resume(db_path: Path):
    db = Database(str(db_path))
    set_database(db)
    with TestClient(app) as first:
        detail = create(first)
        issue(first, detail["id"], 0, 5)
    set_database(None)  # type: ignore[arg-type]

    # Fresh process-equivalent: new Database object on the same file.
    set_database(Database(str(db_path)))
    with TestClient(app) as second:
        segments = second.get(f"/trails/{detail['id']}/segments").json()
        assert len(segments) == 1
        assert segments[0]["end_index"] == 5
        resumed = issue(second, detail["id"], 5, 10)
        assert resumed.status_code == 201
        assert resumed.json()["total_points_used"] == 3
    set_database(None)  # type: ignore[arg-type]


def test_concurrent_same_progress_only_issues_once(client):
    import threading

    detail = create(client, point_limit=20)
    results: list[int] = []

    def attempt():
        results.append(issue(client, detail["id"], 0, 10).status_code)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201] + [409] * 5
    assert len(client.get(f"/trails/{detail['id']}/segments").json()) == 1


def test_unknown_trail_and_segment_404(client):
    assert client.get("/trails/nope").status_code == 404
    assert client.get("/trails/nope/segments").status_code == 404
    detail = create(client)
    assert client.get(f"/trails/{detail['id']}/segments/99").status_code == 404


def test_openapi_and_docs_available(client):
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/trails/{trail_id}/segments" in schema["paths"]
