"""A sketch on a design with no board (QA sweep, `frontend/e2e/qa/`, board-less sketch state).

Cut circle on a board-less design was a 500: the sketch's rebuild looked up `build.board.name`
on a build without a board, and the panel showed "500 Internal Server Error".
"""

from __future__ import annotations

import json

from test_modeling import upward
from test_modeling_boardless import _boardless_box, _keep_config, client  # noqa: F401
from test_sketches import body_for


def test_a_sketch_on_a_design_without_a_board_rebuilds(client) -> None:  # noqa: F811
    sid, build = _boardless_box(client)
    before = len(client.get(f"/api/sessions/{sid}/builds").json())
    plane = upward(json.loads(client.get(build["mesh_url"]).text), build["id"])[0]

    r = client.post(f"/api/sessions/{sid}/sketches", json=body_for(plane))

    assert r.status_code == 201, r.text
    builds = client.get(f"/api/sessions/{sid}/builds").json()
    assert len(builds) == before + 1
    assert builds[-1]["board"] is None
    sketches = client.get(f"/api/sessions/{sid}/sketches").json()
    assert [s["tag"] for s in sketches] == [r.json()["tag"]]
