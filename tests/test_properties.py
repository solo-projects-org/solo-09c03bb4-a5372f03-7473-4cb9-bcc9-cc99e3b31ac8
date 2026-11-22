"""Randomised property tests checked against independent brute force.

The verifier here re-implements the two guarantees in a deliberately
different way from ``app.geometry``:

* error: direct interpolation with :class:`Fraction` at every original time;
* dwell: collect every boundary-crossing time of both curves, then compare the
  inside status on midpoints between consecutive breakpoints (status cannot
  change in between, so this is exact).
"""

from __future__ import annotations

import itertools
import random
from fractions import Fraction

import pytest

from app.geometry import TrailGeometry, chain_via_pins

from .conftest import make_trail_payload


# --------------------------------------------------------------------------- #
# Independent verifier
# --------------------------------------------------------------------------- #
def interp(samples, a, b, tq):
    ta, xa, ya = samples[a]
    tb, xb, yb = samples[b]
    if tq == ta:
        return Fraction(xa), Fraction(ya)
    if tq == tb:
        return Fraction(xb), Fraction(yb)
    lam = Fraction(tq - ta, tb - ta)
    return (Fraction(xa) + lam * (xb - xa), Fraction(ya) + lam * (yb - ya))


def simplified_at(samples, chain, tq):
    for a, b in zip(chain, chain[1:]):
        if samples[a][0] <= tq <= samples[b][0]:
            return interp(samples, a, b, tq)
    raise AssertionError("time outside simplified curve")


def inside(point, zone) -> bool:
    x, y = point
    return zone[0] < x < zone[1] and zone[2] < y < zone[3]


def crossing_times(samples, a, b, zone):
    """Times in (t_a, t_b) where the piece crosses a zone boundary."""
    ta, xa, ya = samples[a]
    tb, xb, yb = samples[b]
    out = set()
    for lo, hi in ((zone[0], zone[1]), (zone[2], zone[3])):
        for boundary in (lo, hi):
            for start, delta in ((xa, xb - xa), (ya, yb - ya)):
                if delta == 0:
                    continue
                lam = Fraction(boundary - start, delta)
                if 0 < lam < 1:
                    out.add(ta + lam * (tb - ta))
    return out


def assert_dwell_equal(samples, chain, zones):
    # Breakpoints must include the crossings of *both* curves; the inside
    # status is constant between consecutive breakpoints.
    breakpoints = {Fraction(samples[i][0]) for i in range(len(samples))}
    for a, b in zip(range(len(samples) - 1), range(1, len(samples))):
        for zone in zones:
            breakpoints |= crossing_times(samples, a, b, zone)
    for a, b in zip(chain, chain[1:]):
        for zone in zones:
            breakpoints |= crossing_times(samples, a, b, zone)
    ordered = sorted(breakpoints)
    probes = list(ordered)
    probes += [(lo + hi) / 2 for lo, hi in zip(ordered, ordered[1:])]
    for zone in zones:
        for tq in probes:
            original = inside(interp(samples, _piece(samples, tq)[0],
                                     _piece(samples, tq)[1], tq), zone)
            simplified = inside(simplified_at(samples, chain, tq), zone)
            assert original == simplified, (tq, zone, chain)


def _piece(samples, tq):
    for a, b in zip(samples, samples[1:]):
        if a[0] <= tq <= b[0]:
            return (samples.index(a), samples.index(b))
    raise AssertionError("time outside original curve")


def assert_error_within(samples, chain, limit: Fraction):
    for a, b in zip(chain, chain[1:]):
        for i in range(a + 1, b):
            px, py = interp(samples, a, b, samples[i][0])
            assert abs(px - samples[i][1]) ** 2 + abs(py - samples[i][2]) ** 2 <= limit**2


def brute_force_min_chain(samples, zones, limit):
    n = len(samples)
    best = None
    for size in range(2, n + 1):
        for middle in itertools.combinations(range(1, n - 1), size - 2):
            chain = [0, *middle, n - 1]
            try:
                assert_error_within(samples, chain, limit)
                assert_dwell_equal(samples, chain, zones)
            except AssertionError:
                continue
            best = chain
            return best
    return best


def random_track(rng, n):
    t = 0
    samples = []
    for _ in range(n):
        t += rng.randint(1, 4)
        samples.append((t, rng.randint(-12, 12), rng.randint(-12, 12)))
    return samples


def random_zones(rng, count):
    zones = []
    for _ in range(count):
        x0 = rng.randint(-10, 8)
        y0 = rng.randint(-10, 8)
        zones.append((x0, x0 + rng.randint(2, 8), y0, y0 + rng.randint(2, 8)))
    return zones


@pytest.mark.parametrize("seed", range(25))
def test_minimal_chain_matches_brute_force(seed):
    rng = random.Random(seed)
    samples = random_track(rng, rng.randint(3, 8))
    zones = random_zones(rng, rng.randint(0, 2))
    limit = Fraction(rng.choice([0, 1, 2, 5, 20]))
    geom = TrailGeometry(
        [s[0] for s in samples],
        [s[1] for s in samples],
        [s[2] for s in samples],
        zones,
        limit,
    )
    chain = geom.minimal_chain(0, len(samples) - 1)
    expected = brute_force_min_chain(samples, zones, limit)
    assert expected is not None  # keeping every sample is always valid
    assert len(chain) == len(expected), (samples, zones, limit, chain, expected)
    assert chain[0] == 0 and chain[-1] == len(samples) - 1
    assert_error_within(samples, chain, limit)
    assert_dwell_equal(samples, chain, zones)


@pytest.mark.parametrize("seed", range(10))
def test_chain_with_pins_matches_brute_force(seed):
    rng = random.Random(1000 + seed)
    samples = random_track(rng, rng.randint(4, 8))
    zones = random_zones(rng, rng.randint(0, 2))
    limit = Fraction(rng.choice([0, 3, 10]))
    pins = rng.sample(range(1, len(samples) - 1), k=rng.randint(1, 2))
    geom = TrailGeometry(
        [s[0] for s in samples],
        [s[1] for s in samples],
        [s[2] for s in samples],
        zones,
        limit,
    )
    chain = chain_via_pins(geom, 0, len(samples) - 1, pins)
    assert set(pins) <= set(chain)
    assert_error_within(samples, chain, limit)
    assert_dwell_equal(samples, chain, zones)
    # Minimality under the pin constraint.
    for size in range(2, len(chain) + 1):
        found = None
        for middle in itertools.combinations(range(1, len(samples) - 1), size - 2):
            candidate = [0, *middle, len(samples) - 1]
            if not set(pins) <= set(candidate):
                continue
            try:
                assert_error_within(samples, candidate, limit)
                assert_dwell_equal(samples, candidate, zones)
            except AssertionError:
                continue
            found = candidate
            break
        assert found is None or len(found) == len(chain)


def test_api_segment_is_valid_and_minimal(client):
    rng = random.Random(42)
    for _ in range(5):
        samples = random_track(rng, 9)
        zones = random_zones(rng, 2)
        limit = Fraction(rng.choice([1, 3, 8]))
        resp = client.post(
            "/trails",
            json=make_trail_payload(
                samples,
                zones=[
                    {"x_min": z[0], "x_max": z[1], "y_min": z[2], "y_max": z[3]}
                    for z in zones
                ],
                error_limit=float(limit),
                point_limit=200,
            ),
        )
        assert resp.status_code == 201, resp.text
        trail = resp.json()
        geom = TrailGeometry(
            [s[0] for s in samples],
            [s[1] for s in samples],
            [s[2] for s in samples],
            zones,
            limit,
        )
        expected = geom.minimal_chain(0, len(samples) - 1)

        seg = client.post(
            f"/trails/{trail['id']}/segments",
            json={"observed_progress": 0, "end_index": 8, "required_pins": []},
        )
        assert seg.status_code == 201, seg.text
        chain = [p["index"] for p in seg.json()["points"]]
        assert chain == expected
        assert_error_within(samples, chain, limit)
        assert_dwell_equal(samples, chain, zones)
