"""End-to-end smoke test against a running server.

Usage:
    python scripts/smoke_http.py [base_url]

Exercises the documented workflow: register a trail, ask for the next
segment, check stale-progress rejection, check point-limit overflow, resume
after a restart-equivalent (fresh HTTP client), and confirm Swagger is served.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def check(label: str, condition: bool, extra="") -> None:
    print(("PASS  " if condition else "FAIL  ") + label + (f"  {extra}" if extra else ""))
    if not condition:
        raise SystemExit(1)


def main() -> None:
    status, _ = call("GET", "/healthz")
    check("healthz", status == 200)

    with urllib.request.urlopen(BASE + "/docs", timeout=10) as resp:
        check("swagger /docs", resp.status == 200 and b"swagger" in resp.read().lower())
    status, schema = call("GET", "/openapi.json")
    check("openapi", status == 200 and "/trails/{trail_id}/segments" in schema["paths"])

    # 21 samples, straight then a dip through a zone, then straight.
    samples = [{"t": t, "x": 5 * t, "y": 0} for t in range(21)]
    for i in range(8, 13):
        samples[i]["y"] = (i - 8) * 4 if i <= 10 else (12 - i) * 4
    payload = {
        "name": "smoke-wetland",
        "samples": samples,
        "zones": [
            {"name": "reed-bed", "x_min": 30, "x_max": 70, "y_min": -10, "y_max": 10}
        ],
        "error_limit": "2.5",
        "point_limit": 12,
    }
    status, trail = call("POST", "/trails", payload)
    check("register trail", status == 201, f"id={trail.get('id')}")
    trail_id = trail["id"]

    status, first = call(
        "POST",
        f"/trails/{trail_id}/segments",
        {"observed_progress": 0, "end_index": 10, "required_pins": [9]},
    )
    check("issue first segment", status == 201, f"points={[p['index'] for p in first['points']]}")
    check("pin retained", 9 in [p["index"] for p in first["points"]])
    check("tail retained", first["points"][-1]["index"] == 10)

    status, err = call(
        "POST",
        f"/trails/{trail_id}/segments",
        {"observed_progress": 0, "end_index": 20, "required_pins": []},
    )
    check(
        "stale progress rejected",
        status == 409 and err["detail"]["code"] == "stale_progress",
        f"current={err['detail'].get('current_progress')}",
    )

    status, second = call(
        "POST",
        f"/trails/{trail_id}/segments",
        {"observed_progress": 10, "end_index": 20, "required_pins": []},
    )
    check("issue second segment", status == 201, f"points={[p['index'] for p in second['points']]}")
    check("total counts seam once", second["total_points_used"] == len(first["points"]) + len(second["points"]) - 1)

    status, listed = call("GET", f"/trails/{trail_id}/segments")
    check("segments persisted", status == 200 and len(listed) == 2)

    status, err = call(
        "POST",
        f"/trails/{trail_id}/segments",
        {"observed_progress": 20, "end_index": 20, "required_pins": []},
    )
    check("complete trail rejected", status == 409 and err["detail"]["code"] == "trail_complete")

    # A second trail whose minimum exceeds the point limit.
    tight = [{"t": t, "x": t, "y": t * t % 7} for t in range(9)]
    status, t2 = call(
        "POST",
        "/trails",
        {"samples": tight, "zones": [], "error_limit": 0, "point_limit": 3},
    )
    check("register tight trail", status == 201)
    status, err = call(
        "POST",
        f"/trails/{t2['id']}/segments",
        {"observed_progress": 0, "end_index": 8, "required_pins": []},
    )
    check(
        "point limit overflow reported",
        status == 409 and err["detail"]["code"] == "point_limit_exceeded",
        f"needs={err['detail'].get('minimum_total_points')}",
    )
    status, t2_now = call("GET", f"/trails/{t2['id']}")
    check("progress unchanged after overflow", status == 200 and t2_now["progress_index"] == 0)

    print("\nall smoke checks passed")


if __name__ == "__main__":
    main()
