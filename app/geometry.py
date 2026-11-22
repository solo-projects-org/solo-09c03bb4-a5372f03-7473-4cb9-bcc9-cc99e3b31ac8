"""Exact geometry for track segmentation.

All arithmetic that decides whether a candidate segment is acceptable is done
with integers or :class:`fractions.Fraction`; no float rounding is involved.

Model
-----
* Original track: ``n`` samples ``(t_i, x_i, y_i)`` with strictly increasing
  integer seconds and integer metre coordinates.
* Simplified track: the polyline through a chosen subset of the original
  samples (only original samples may be vertices).  Both curves are linearly
  interpolated in *time*.
* Error limit: at every original sampling instant the Euclidean distance
  between the two curves may not exceed ``error_limit``.
* Dwell: for every management zone (open axis-aligned rectangle -- the
  boundary is not inside) the set of times at which the trajectory is inside
  the zone must be identical for the original and the simplified curve.

A chord between two selected samples only affects the time span between them,
so both constraints decompose over the ``gap`` between consecutive selected
points; :meth:`TrailGeometry.edge_valid` therefore only inspects one gap.
"""

from __future__ import annotations

import bisect
from fractions import Fraction

Rect = tuple[int, int, int, int]  # x_min, x_max, y_min, y_max (open rectangle)

# A component of the "inside" time set.  The set is open (the rectangle
# boundary is excluded), so both ends are open; the two boolean flags only
# record whether the piece-level endpoint belongs to the inside set and are
# used while merging pieces, never in the final output.
_Component = tuple[Fraction, bool, Fraction, bool]


class TrailGeometry:
    """Pre-computed geometry plus memoised edge/chain queries for one track."""

    def __init__(
        self,
        times: list[int],
        xs: list[int],
        ys: list[int],
        rects: list[Rect],
        error_limit: Fraction,
    ) -> None:
        if not (len(times) == len(xs) == len(ys)):
            raise ValueError("times, xs and ys must have the same length")
        if len(times) < 2:
            raise ValueError("a track needs at least two samples")
        self.t = times
        self.x = xs
        self.y = ys
        self.n = len(times)
        self.rects = rects
        self.error_limit = error_limit
        self.err_num = error_limit.numerator
        self.err_den = error_limit.denominator

        # Global inside-time components of the original curve, per zone.
        self.components: list[list[tuple[Fraction, Fraction]]] = [
            self._build_components(rect) for rect in rects
        ]
        self.component_ends: list[list[Fraction]] = [
            [hi for _lo, hi in comps] for comps in self.components
        ]

        self._edge_cache: dict[tuple[int, int], bool] = {}
        self._chain_cache: dict[tuple[int, int], list[int] | None] = {}

    # ------------------------------------------------------------------ #
    # Interval primitives
    # ------------------------------------------------------------------ #
    @staticmethod
    def _axis_bounds(
        start: int, delta: int, lo: int, hi: int
    ) -> tuple[Fraction | None, Fraction | None] | None:
        """Solve ``lo < start + lam*delta < hi`` for ``lam``.

        Returns ``(a, b)`` where either bound may be ``None`` (infinite), or
        ``None`` when the strict inequality has no solution.
        """
        if delta == 0:
            return (None, None) if lo < start < hi else None
        a = Fraction(lo - start, delta)
        b = Fraction(hi - start, delta)
        return (a, b) if delta > 0 else (b, a)

    def _piece_inside(
        self,
        t0: int,
        x0: int,
        y0: int,
        t1: int,
        x1: int,
        y1: int,
        rect: Rect,
    ) -> _Component | None:
        """Time interval of one linear piece strictly inside ``rect``.

        Returns ``(lo, lo_in, hi, hi_in)`` in absolute time.  ``lo_in`` /
        ``hi_in`` state whether the piece's own endpoints ``t0`` / ``t1`` are
        inside; a point exactly on the boundary is *not* inside, so a crossing
        in the interior is always open.
        """
        rx_min, rx_max, ry_min, ry_max = rect
        bounds_x = self._axis_bounds(x0, x1 - x0, rx_min, rx_max)
        if bounds_x is None:
            return None
        bounds_y = self._axis_bounds(y0, y1 - y0, ry_min, ry_max)
        if bounds_y is None:
            return None
        a, b = bounds_x
        c, d = bounds_y
        # Intersect the two open intervals (a, b) and (c, d).
        lo = a if c is None else (c if a is None else max(a, c))
        hi = b if d is None else (d if b is None else min(b, d))
        if hi is not None and hi <= 0:
            return None
        if lo is not None and lo >= 1:
            return None
        if lo is None or lo < 0:
            lam_lo, lo_in = Fraction(0), True
        elif lo == 0:
            lam_lo, lo_in = Fraction(0), False
        else:
            lam_lo, lo_in = lo, False
        if hi is None or hi > 1:
            lam_hi, hi_in = Fraction(1), True
        elif hi == 1:
            lam_hi, hi_in = Fraction(1), False
        else:
            lam_hi, hi_in = hi, False
        if lam_lo >= lam_hi:
            return None
        dt = t1 - t0
        return (
            Fraction(t0) + lam_lo * dt,
            lo_in,
            Fraction(t0) + lam_hi * dt,
            hi_in,
        )

    def _build_components(self, rect: Rect) -> list[tuple[Fraction, Fraction]]:
        """Merge per-piece inside intervals into global open components."""
        merged: list[list] = []
        for i in range(self.n - 1):
            sol = self._piece_inside(
                self.t[i], self.x[i], self.y[i],
                self.t[i + 1], self.x[i + 1], self.y[i + 1],
                rect,
            )
            if sol is None:
                continue
            lo, lo_in, hi, hi_in = sol
            if merged:
                last = merged[-1]
                same_endpoint = lo == last[2]
                # Pieces are contiguous in time; at a shared sample both flags
                # carry the same truth value, so ``lo_in or last[3]`` is
                # exactly "the shared sample is inside".
                if lo < last[2] or (same_endpoint and (lo_in or last[3])):
                    if hi > last[2] or (hi == last[2] and hi_in):
                        last[2] = hi
                        last[3] = hi_in
                    continue
            merged.append([lo, lo_in, hi, hi_in])
        return [(item[0], item[2]) for item in merged]

    def _original_inside(
        self, zone: int, ta: Fraction, tb: Fraction
    ) -> list[tuple[Fraction, Fraction]]:
        """Original inside-set restricted to the open gap ``(ta, tb)``."""
        comps = self.components[zone]
        if not comps:
            return []
        ends = self.component_ends[zone]
        j = bisect.bisect_right(ends, ta)
        clipped: list[tuple[Fraction, Fraction]] = []
        while j < len(comps) and comps[j][0] < tb:
            lo = max(comps[j][0], ta)
            hi = min(comps[j][1], tb)
            if lo < hi:
                clipped.append((lo, hi))
                if len(clipped) > 1:
                    # A chord contributes at most one interval in the gap.
                    return clipped
            j += 1
        return clipped

    def _chord_inside(
        self, a: int, b: int, zone: int
    ) -> list[tuple[Fraction, Fraction]]:
        """Simplified chord inside-set restricted to the open gap."""
        ta = Fraction(self.t[a])
        tb = Fraction(self.t[b])
        sol = self._piece_inside(
            self.t[a], self.x[a], self.y[a],
            self.t[b], self.x[b], self.y[b],
            self.rects[zone],
        )
        if sol is None:
            return []
        lo = max(sol[0], ta)
        hi = min(sol[2], tb)
        return [(lo, hi)] if lo < hi else []

    # ------------------------------------------------------------------ #
    # Validity
    # ------------------------------------------------------------------ #
    def _error_ok(self, a: int, b: int) -> bool:
        """Exact check: every original sample in (a, b) is within the limit."""
        t, x, y = self.t, self.x, self.y
        span = t[b] - t[a]
        dx = x[b] - x[a]
        dy = y[b] - y[a]
        den2 = self.err_den * self.err_den
        lim2 = self.err_num * self.err_num * span * span
        for i in range(a + 1, b):
            dt = t[i] - t[a]
            nx = (x[i] - x[a]) * span - dx * dt
            ny = (y[i] - y[a]) * span - dy * dt
            if (nx * nx + ny * ny) * den2 > lim2:
                return False
        return True

    def _dwell_ok(self, a: int, b: int) -> bool:
        ta = Fraction(self.t[a])
        tb = Fraction(self.t[b])
        for zone in range(len(self.rects)):
            if self._original_inside(zone, ta, tb) != self._chord_inside(a, b, zone):
                return False
        return True

    def edge_valid(self, a: int, b: int) -> bool:
        """Can a single chord go from sample ``a`` to sample ``b``?"""
        if not (0 <= a < b < self.n):
            return False
        key = (a, b)
        cached = self._edge_cache.get(key)
        if cached is not None:
            return cached
        if b == a + 1:
            # The chord is exactly the original piece: zero error, same dwell.
            self._edge_cache[key] = True
            return True
        ok = self._error_ok(a, b) and self._dwell_ok(a, b)
        self._edge_cache[key] = ok
        return ok

    # ------------------------------------------------------------------ #
    # Minimal chains
    # ------------------------------------------------------------------ #
    def minimal_chain(self, a: int, b: int) -> list[int] | None:
        """Fewest original indices from ``a`` to ``b`` (both included).

        Among all minimum-size chains the lexicographically smallest index
        list is returned, which makes issuance deterministic.
        """
        if not (0 <= a <= b < self.n):
            raise ValueError("bad chain range")
        if a == b:
            return [a]
        key = (a, b)
        if key in self._chain_cache:
            cached = self._chain_cache[key]
            return None if cached is None else list(cached)

        inf = 10**9
        dp = [inf] * (b + 1)
        dp[b] = 1
        for i in range(b - 1, a - 1, -1):
            best = inf
            for j in range(i + 1, b + 1):
                if dp[j] + 1 < best and self.edge_valid(i, j):
                    best = dp[j] + 1
            dp[i] = best
        if dp[a] >= inf:  # pragma: no cover - adjacent edges are always valid
            self._chain_cache[key] = None
            return None

        path = [a]
        cur = a
        while cur < b:
            want = dp[cur] - 1
            for j in range(cur + 1, b + 1):
                if dp[j] == want and self.edge_valid(cur, j):
                    path.append(j)
                    cur = j
                    break
            else:  # pragma: no cover - DP guarantees a successor
                raise RuntimeError("broken DP reconstruction")
        self._chain_cache[key] = path
        return list(path)

    def minimum_points(self, a: int, b: int) -> int | None:
        chain = self.minimal_chain(a, b)
        return None if chain is None else len(chain)


def chain_via_pins(
    geom: TrailGeometry, start: int, end: int, pins: list[int]
) -> list[int]:
    """Minimum chain from ``start`` to ``end`` that includes every pin.

    Constraints decompose per gap, so concatenating the minimum chains
    between consecutive anchors is globally optimal.
    """
    anchors = [start]
    anchors.extend(sorted({p for p in pins if start < p < end}))
    anchors.append(end)
    selected: list[int] = [start]
    for left, right in zip(anchors, anchors[1:]):
        sub = geom.minimal_chain(left, right)
        if sub is None:  # pragma: no cover - adjacent edges always exist
            raise ValueError("no feasible chain")
        selected.extend(sub[1:])
    return selected
