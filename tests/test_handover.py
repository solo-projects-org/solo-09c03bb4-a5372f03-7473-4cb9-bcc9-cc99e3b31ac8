"""Multi-segment handover invariants checked end to end through the API."""

from __future__ import annotations

import random
from fractions import Fraction

from .conftest import make_trail_payload
from .test_properties import (
    assert_dwell_equal,
    assert_error_within,
    random_track,
    random_zones,
)


def _register(client, samples, zones, limit, point_limit, pins_limit=200):
    resp = client.post(
        "/trails",
        json=make_trail_payload(
            samples,
            zones=[
                {"x_min": z[0], "x_max": z[1], "y_min": z[2], "y_max": z[3]}
                for z in zones
            ],
            error_limit=float(limit),
            point_limit=point_limit,
        ),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _issue(client, trail_id, progress, end, pins=()):
    return client.post(
        f"/trails/{trail_id}/segments",
        json={
            "observed_progress": progress,
            "end_index": end,
            "required_pins": list(pins),
        },
    )


def test_full_handover_preserves_all_guarantees(client):
    rng = random.Random(2024)
    samples = random_track(rng, 25)
    zones = random_zones(rng, 3)
    limit = Fraction("2.5")
    trail = _register(client, samples, zones, limit, point_limit=200)
    trail_id = trail["id"]
    n = len(samples)

    # Issue the trail in irregular chunks with occasional review pins.
    rng2 = random.Random(99)
    progress = 0
    issued: list[dict] = []
    while progress < n - 1:
        end = min(n - 1, progress + rng2.randint(1, 6))
        pins = (
            [rng2.randint(progress + 1, end - 1)]
            if end - progress > 2 and rng2.random() < 0.5
            else []
        )
        resp = _issue(client, trail_id, progress, end, pins)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["start_index"] == progress
        assert body["points"][0]["index"] == progress
        assert body["points"][-1]["index"] == end
        assert set(pins) <= {p["index"] for p in body["points"]}
        issued.append(body)
        progress = end

    # Seam counted exactly once across the whole handover.
    total = sum(seg["new_point_count"] for seg in issued)
    assert total == issued[-1]["total_points_used"]
    assert total == sum(seg["point_count"] for seg in issued) - (len(issued) - 1)

    # Every returned point is an original sample, unchanged.
    for seg in issued:
        for point in seg["points"]:
            i = point["index"]
            assert (point["t"], point["x"], point["y"]) == samples[i]

    # The concatenation is a valid simplification of the whole track.
    chain = [seg["points"][0]["index"] for seg in issued]
    chain += [seg["points"][-1]["index"] for seg in issued]
    chain = sorted(set(chain))
    # Interleave each segment's interior points back in order.
    full = [issued[0]["points"][0]["index"]]
    for seg in issued:
        full.extend(p["index"] for p in seg["points"][1:])
    assert chain[0] == 0 and chain[-1] == n - 1
    assert set(full) >= set(chain)
    assert_error_within(samples, full, limit)
    assert_dwell_equal(samples, full, zones)

    # Progress and summary agree after the handover.
    detail = client.get(f"/trails/{trail_id}").json()
    assert detail["progress_index"] == n - 1
    assert detail["issued_segment_count"] == len(issued)
    assert detail["issued_point_count"] == total
    assert detail["remaining_point_budget"] == 200 - total
    assert detail["minimum_total_points"] == total


def test_pins_on_seam_do_not_add_points(client):
    samples = [(t, 2 * t, 0) for t in range(6)]
    trail = _register(client, samples, [], 0, point_limit=10)
    resp = _issue(client, trail["id"], 0, 5, [0, 5])
    assert resp.status_code == 201
    body = resp.json()
    assert [p["index"] for p in body["points"]] == [0, 5]
    assert body["point_count"] == 2


def test_each_segment_uses_fewest_points_for_its_span(client):
    # A dip in the middle means sample 2 must be kept when it is the segment
    # end, but a much later segment end may skip it only if the whole span
    # stays within the error limit -- here it cannot, so it is kept.
    samples = [(0, 0, 0), (1, 1, 0), (2, 2, 6), (3, 3, 0), (4, 4, 0)]
    trail = _register(client, samples, [], 4, point_limit=10)
    # 0 -> 1 -> 2 is needed (dip at index 2 is 6 m above the chord 0-4).
    body = _issue(client, trail["id"], 0, 4).json()
    assert [p["index"] for p in body["points"]] == [0, 2, 4]

    # Same trail with a larger limit collapses to endpoints.
    trail2 = _register(client, samples, [], 6, point_limit=10)
    body2 = _issue(client, trail2["id"], 0, 4).json()
    assert [p["index"] for p in body2["points"]] == [0, 4]


def test_budget_guard_uses_future_minimum_not_just_current_segment(client):
    # Segment 0..1 costs 2 points. The rest needs 4 more points; with a limit
    # of 4 the request must fail even though the current segment is tiny.
    samples = [(0, 0, 0), (1, 0, 10), (2, 0, 0), (3, 0, 10), (4, 0, 0)]
    trail = _register(client, samples, [], 0, point_limit=4)
    resp = _issue(client, trail["id"], 0, 1)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "point_limit_exceeded"
    assert detail["segment_minimum_points"] == 2
    assert detail["remaining_minimum_points"] == 4
    assert detail["minimum_total_points"] == 5
    # Progress untouched.
    assert client.get(f"/trails/{trail['id']}").json()["progress_index"] == 0


def test_duplicate_and_overlapping_pins_are_normalised(client):
    samples = [(t, 5 * t, 0) for t in range(8)]
    trail = _register(client, samples, [], 0, point_limit=10)
    body = _issue(client, trail["id"], 0, 7, [3, 3, 5, 5]).json()
    assert [p["index"] for p in body["points"]] == [0, 3, 5, 7]
    assert body["required_pins"] == [3, 5]


def test_long_track_stays_under_limits(client):
    rng = random.Random(5)
    samples = random_track(rng, 200)
    zones = random_zones(rng, 4)
    trail = _register(client, samples, zones, 1, point_limit=200)
    body = _issue(client, trail["id"], 0, 199)
    assert body.status_code in (201, 409)
    if body.status_code == 201:
        chain = [p["index"] for p in body.json()["points"]]
        assert_error_within(samples, chain, Fraction(1))
        assert_dwell_equal(samples, chain, zones)
