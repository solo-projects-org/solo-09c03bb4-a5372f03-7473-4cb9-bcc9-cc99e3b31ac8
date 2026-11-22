"""Unit tests for the exact geometry helpers."""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.geometry import TrailGeometry, chain_via_pins


def geom(samples, zones=(), limit=0):
    return TrailGeometry(
        [s[0] for s in samples],
        [s[1] for s in samples],
        [s[2] for s in samples],
        list(zones),
        Fraction(limit),
    )


def test_collinear_track_collapses_to_endpoints():
    g = geom([(0, 0, 0), (1, 3, 3), (2, 6, 6), (3, 9, 9)])
    assert g.minimal_chain(0, 3) == [0, 3]
    assert g.minimum_points(0, 3) == 2


def test_error_limit_is_exact_and_inclusive():
    # Middle sample sits exactly 2 m (vertical) off the chord.
    samples = [(0, 0, 0), (1, 5, 2), (2, 10, 0)]
    assert geom(samples, limit=2).edge_valid(0, 2) is True
    assert geom(samples, limit=1.999).edge_valid(0, 2) is False
    # Exact rational arithmetic: 2 +/- 1e-19 flips the verdict.
    assert geom(samples, limit="2.0000000000000000001").edge_valid(0, 2) is True
    assert geom(samples, limit="1.9999999999999999999").edge_valid(0, 2) is False


def test_time_interpolation_uses_time_not_index():
    # Samples are not evenly spaced; the chord is interpolated in time.
    samples = [(0, 0, 0), (10, 10, 5), (20, 20, 0)]
    # At t=10 the chord is at y=0 and the sample at y=5 -> 5 m error.
    assert geom(samples, limit=5).edge_valid(0, 2) is True
    assert geom(samples, limit=4.9).edge_valid(0, 2) is False


def test_dwell_requires_keeping_zone_entry_and_exit():
    # Track dips into the zone then leaves; endpoints outside.
    zone = (-5, 5, -5, 5)  # x_min, x_max, y_min, y_max
    samples = [(0, 0, -10), (1, 0, 0), (2, 0, -10)]
    g = geom(samples, zones=[zone], limit=1000)
    assert g.edge_valid(0, 2) is False  # chord stays at y=-10
    assert g.minimal_chain(0, 2) == [0, 1, 2]


def test_zone_boundary_is_not_inside():
    # The straight line y=0 rides exactly on the zone boundary y_min=0.
    zone = (0, 10, 0, 10)
    samples = [(0, 1, 0), (1, 9, 0)]
    g = geom(samples, zones=[zone], limit=0)
    assert g.components == [[]]
    # Endpoints outside (on boundary); no inside time -> chord is fine.
    assert g.edge_valid(0, 1) is True


def test_zone_crossing_exactly_matches():
    zone = (0, 10, 0, 10)
    samples = [(0, 5, -5), (1, 5, 5), (2, 5, 15)]
    g = geom(samples, zones=[zone], limit=0)
    # Original enters at t=0.5 and leaves at t=1.5.
    assert g.components == [[(Fraction(1, 2), Fraction(3, 2))]]
    assert g.edge_valid(0, 2) is True


def test_two_zone_visits_cannot_be_collapsed():
    zone = (0, 10, 0, 10)
    samples = [
        (0, 5, -5), (1, 5, 5), (2, 5, -5), (3, 5, 5), (4, 5, -5),
    ]
    g = geom(samples, zones=[zone], limit=1000)
    assert len(g.components[0]) == 2
    assert g.edge_valid(0, 4) is False


def test_pins_force_inclusion_and_minimality():
    g = geom([(0, 0, 0), (1, 1, 0), (2, 2, 0), (3, 3, 0), (4, 4, 0)])
    assert chain_via_pins(g, 0, 4, []) == [0, 4]
    assert chain_via_pins(g, 0, 4, [2]) == [0, 2, 4]
    assert chain_via_pins(g, 0, 4, [1, 3]) == [0, 1, 3, 4]


def test_chain_is_cache_stable_and_lexicographic():
    g = geom([(0, 0, 0), (1, 0, 5), (2, 0, 0)], limit=5)
    first = g.minimal_chain(0, 2)
    assert first == [0, 2]
    assert g.minimal_chain(0, 2) == first


@pytest.mark.parametrize("limit", ["0", "0.5", "2", "10.25"])
def test_adjacent_edges_always_valid(limit):
    g = geom([(0, 0, 0), (1, 100, 100), (2, -50, 3)], limit=limit)
    assert g.edge_valid(0, 1) is True
    assert g.edge_valid(1, 2) is True


def test_riding_a_zone_edge_is_never_inside():
    zone = (0, 10, 0, 10)
    samples = [(0, 0, 1), (1, 0, 5), (2, 0, 9)]  # x == x_min the whole way
    g = geom(samples, zones=[zone], limit=0)
    assert g.components == [[]]
    assert g.minimal_chain(0, 2) == [0, 2]


def test_touching_a_zone_corner_is_not_entering():
    zone = (0, 10, 0, 10)
    # Line y = -x grazes the corner (0,0) and is outside either side.
    samples = [(0, -2, 2), (1, 0, 0), (2, 2, -2)]
    g = geom(samples, zones=[zone], limit=0)
    assert g.components == [[]]
    assert g.edge_valid(0, 2) is True
    assert g.minimal_chain(0, 2) == [0, 2]


def test_peak_inside_zone_must_be_kept():
    zone = (0, 10, 0, 10)
    samples = [(0, 5, -1), (1, 5, 5), (2, 5, -1)]
    g = geom(samples, zones=[zone], limit=0)
    assert g.edge_valid(0, 2) is False
    assert g.minimal_chain(0, 2) == [0, 1, 2]
