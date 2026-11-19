"""Where parameters are measured on the model (`app.dimensions`): `Parameter.dimension` inferred
from a build's mesh, so the canvas's Dimensions view has lines to draw on any design.

Unit tests build small meshes by hand (boxes, cylinders and cones); the API tests read the
demos in `demos/sessions/`, their originals and their copies, on the real API and the mock.
"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from app.contracts.mesh import AssetExtras, MeshFace, MeshInfo, PartInfo
from app.contracts.session import Dimension, DimensionEnd, Drag, Parameter
from app.dimensions import grouped_values, measured, sites_of, tolerance, with_dimensions

# --- Hand-made meshes ---------------------------------------------------------------------------

Face = tuple[str, str, list[tuple[float, float, float]], list[tuple[int, int, int]]]


def box_faces(name: str, lo: tuple, hi: tuple, feature: str | None = None) -> list[Face]:
    """The six faces of an axis-aligned box, `<name>.<axis><lo|hi>`."""
    out = []
    for axis in range(3):
        u, v = [i for i in range(3) if i != axis]
        for side, level in (("lo", lo[axis]), ("hi", hi[axis])):
            pts = []
            for a, b in ((lo[u], lo[v]), (hi[u], lo[v]), (hi[u], hi[v]), (lo[u], hi[v])):
                p = [0.0, 0.0, 0.0]
                p[axis], p[u], p[v] = level, a, b
                pts.append(tuple(p))
            # Wound so the normal points out of the box.
            tris = (
                [(0, 1, 2), (0, 2, 3)] if (side == "hi") == (axis != 1) else [(0, 2, 1), (0, 3, 2)]
            )
            out.append((f"{name}.{'xyz'[axis]}{side}", feature or name, pts, tris))
    return out


def round_face(
    face_id: str,
    feature: str,
    centre: tuple[float, float],
    radii: tuple[float, float],
    z: tuple[float, float],
    arc: tuple[float, float] = (0.0, 2 * math.pi),
    steps: int = 24,
) -> Face:
    """The side of a cylinder (equal radii) or cone about Z: two rings joined by triangles."""
    pts = []
    full = abs(arc[1] - arc[0] - 2 * math.pi) < 1e-9
    n = steps if full else steps + 1
    for r, level in zip(radii, z, strict=True):
        for i in range(n):
            a = arc[0] + (arc[1] - arc[0]) * i / steps
            pts.append((centre[0] + r * math.cos(a), centre[1] + r * math.sin(a), level))
    tris = []
    for i in range(steps):
        j = (i + 1) % n
        tris += [(i, j, n + j), (i, n + j, n + i)]
    return (face_id, feature, pts, tris)


def mesh(parts: dict[str, list[Face]], kinds: dict[str, str] | None = None) -> MeshInfo:
    infos, face_parts, features = {}, {}, set()
    for node, (name, faces) in enumerate(parts.items()):
        positions, owner, indices, listed = [], [], [], []
        for i, (fid, feature, pts, tris) in enumerate(faces):
            base = len(positions)
            positions += pts
            owner += [i] * len(pts)
            indices += [base + k for t in tris for k in t]
            listed.append(MeshFace(id=fid, feature=feature))
            face_parts[fid] = name
            features.add(feature)
        infos[name] = PartInfo(
            name=name,
            node=node,
            vertex_count=len(positions),
            triangle_count=len(indices) // 3,
            faces=listed,
            edges=[],
            positions=positions,
            face_index=owner,
            kind=(kinds or {}).get(name, "enclosure"),
            indices=indices,
        )
    asset = AssetExtras(contract="mesh/1", units="mm", up_axis="Z")
    return MeshInfo(infos, face_parts, {}, features, asset)


def param(name: str, value: float | str, unit: str | None = "mm", **kw) -> Parameter:
    return Parameter(name=name, value=value, unit=unit, locked=False, **kw)


def by_name(params: list[Parameter]) -> dict[str, Parameter]:
    return {p.name: p for p in params}


BOX = mesh({"box": box_faces("box", (0.0, -20.0, 0.0), (60.0, 20.0, 30.0))})


# --- Overall extents ----------------------------------------------------------------------------


def test_each_extent_is_the_distance_between_the_outermost_faces_on_its_axis() -> None:
    out = by_name(
        with_dimensions(
            [param("length", 60.0), param("width", 40.0), param("height", 30.0)], sites_of(BOX)
        )
    )
    for name, axis, faces in (
        ("length", 0, ("box.xlo", "box.xhi")),
        ("width", 1, ("box.ylo", "box.yhi")),
        ("height", 2, ("box.zlo", "box.zhi")),
    ):
        dim = out[name].dimension
        assert dim is not None and dim.kind == "distance"
        assert (dim.start.face, dim.end.face) == faces
        assert dim.start.part == dim.end.part == "box"
        start, end = dim.start.point, dim.end.point
        # Parallel to the axis, as long as the value, each end on its face's plane.
        assert all(abs(start[i] - end[i]) < 1e-9 for i in range(3) if i != axis)
        assert end[axis] - start[axis] == pytest.approx(out[name].value)
        assert out[name].drag is None


def test_the_extent_spans_every_enclosure_part_but_not_the_inserts() -> None:
    two = mesh(
        {
            "base": box_faces("base", (0, 0, -7.5), (40, 50, 0)),
            "lid": box_faces("lid", (0, 0, 0), (40, 50, 7.6)),
            "insert": box_faces("insert", (10, 10, -9), (12, 12, 9)),
        },
        kinds={"insert": "insert"},
    )
    dim = by_name(with_dimensions([param("height", 15.1)], sites_of(two)))["height"].dimension
    assert dim is not None
    assert (dim.start.part, dim.start.face) == ("base", "base.zlo")
    assert (dim.end.part, dim.end.face) == ("lid", "lid.zhi")


def test_a_size_word_wins_an_axis_several_parameters_match() -> None:
    params = [param("a_gap", 60.0), param("length", 60.0, description="")]
    out = by_name(with_dimensions(params, sites_of(BOX)))
    assert out["length"].dimension is not None
    assert out["a_gap"].dimension is None  # one dimension per axis


def test_a_description_counts_and_a_level_loses_to_a_size() -> None:
    params = [param("z_top", 30.0), param("roof", 30.0, description="Overall height")]
    out = by_name(with_dimensions(params, sites_of(BOX)))
    assert out["roof"].dimension is not None and out["z_top"].dimension is None


def test_with_no_size_word_the_name_decides_and_the_choice_is_stable() -> None:
    params = [param("b", 60.0), param("a", 60.0)]
    first = by_name(with_dimensions(params, sites_of(BOX)))
    again = by_name(with_dimensions(list(reversed(params)), sites_of(BOX)))
    assert first["a"].dimension is not None and first["b"].dimension is None
    assert again["a"].dimension == first["a"].dimension and again["b"].dimension is None


def test_a_cube_gives_each_size_its_own_axis_and_height_the_vertical() -> None:
    cube = mesh({"c": box_faces("c", (0, 0, 0), (30, 30, 30))})
    params = [param("width", 30.0), param("length", 30.0), param("height", 30.0)]
    out = by_name(with_dimensions(params, sites_of(cube)))
    axes = {}
    for p in out.values():
        assert p.dimension is not None
        a, b = p.dimension.start.point, p.dimension.end.point
        axes[p.name] = next(i for i in range(3) if abs(a[i] - b[i]) > 1)
    assert axes["height"] == 2 and sorted(axes.values()) == [0, 1, 2]


@pytest.mark.parametrize(
    ("value", "hit"),
    [
        (5.0, True),
        (5.0099, True),  # under 0.01 mm
        (4.9901, True),
        (5.0101, False),  # over 0.01 mm (0.1 % of 5 mm is less)
        (4.9899, False),
    ],
)
def test_tolerance_is_0_01_mm_on_a_small_size(value: float, hit: bool) -> None:
    small = mesh({"s": box_faces("s", (0, 0, 0), (5, 70, 90))})
    out = with_dimensions([param("thickness", value)], sites_of(small))
    assert (out[0].dimension is not None) is hit


@pytest.mark.parametrize(("value", "hit"), [(60.059, True), (59.941, True), (60.061, False)])
def test_tolerance_is_0_1_percent_on_a_large_size(value: float, hit: bool) -> None:
    assert tolerance(60.0) == pytest.approx(0.06)
    out = with_dimensions([param("length", value)], sites_of(BOX))
    assert (out[0].dimension is not None) is hit


def test_a_drafted_wall_is_measured_where_it_reaches_furthest() -> None:
    # A wall leaning in by 1 mm over its height: its bottom edge is the extent.
    pts = [(60, -20, 0), (60, 20, 0), (59, 20, 30), (59, -20, 30)]
    faces = [f for f in box_faces("t", (0, -20, 0), (60, 20, 30)) if f[0] != "t.xhi"]
    faces.append(("t.wall", "t", pts, [(0, 1, 2), (0, 2, 3)]))
    out = with_dimensions([param("length", 60.0)], sites_of(mesh({"t": faces})))
    dim = out[0].dimension
    assert dim is not None and dim.end.face == "t.wall"
    assert dim.end.point[0] == 60 and dim.end.point[2] == 0
    assert dim.start.point[1:] == dim.end.point[1:]


# --- Radii and diameters ------------------------------------------------------------------------

BOSSES = mesh(
    {
        "lid": [
            *box_faces("lid", (-20, -20, 0), (20, 20, 8)),
            round_face("lid.boss_2.0", "boss_2", (5.0, 0.0), (4.5, 4.5), (1.0, 5.0)),
            round_face("lid.boss_1.0", "boss_1", (-5.0, 0.0), (4.5, 4.5), (1.0, 5.0)),
            # A drafted peg: 1.2 mm at its tip, 1.3 mm at its root.
            round_face("lid.peg_H1.0", "peg_H1", (0.0, 10.0), (1.2, 1.3), (1.0, 5.0)),
            # A rounded corner: a quarter of a cylinder.
            round_face("lid.corner", "lid", (15, 15), (5, 5), (0, 8), arc=(0, math.pi / 2)),
            round_face("lid.hole_1.0", "hole_1", (0.0, -10.0), (2.25, 2.25), (0.0, 8.0)),
        ]
    }
)


def _radius(dim: Dimension | None) -> float:
    assert dim is not None
    return math.dist(dim.start.point, dim.end.point)


def test_a_feature_radius_runs_from_the_axis_to_the_round_face() -> None:
    out = by_name(
        with_dimensions(
            [param("boss_1_radius", 4.5), param("r", 4.5, feature="boss_2")], sites_of(BOSSES)
        )
    )
    dim = out["boss_1_radius"].dimension
    assert dim is not None and dim.kind == "radius"
    assert dim.start.face is None and dim.start.part is None  # the centre, in open space
    assert (dim.end.part, dim.end.face) == ("lid", "lid.boss_1.0")
    assert dim.start.point[:2] == pytest.approx((-5.0, 0.0), abs=1e-4)
    assert dim.start.point[2] == dim.end.point[2]  # square to the axis
    assert _radius(dim) == pytest.approx(4.5, abs=1e-4)
    assert out["r"].dimension is not None and out["r"].dimension.end.face == "lid.boss_2.0"


def test_a_kind_of_feature_takes_its_first_one() -> None:
    out = with_dimensions([param("boss.radius", 4.5)], sites_of(BOSSES))
    assert out[0].dimension is not None and out[0].dimension.end.face == "lid.boss_1.0"


def test_a_cone_is_measured_at_the_end_whose_radius_it_is() -> None:
    sites = sites_of(BOSSES)
    for value, z in ((1.2, 1.0), (1.3, 5.0)):
        out = with_dimensions([param("peg_H1_radius", value)], sites)
        assert _radius(out[0].dimension) == pytest.approx(value, abs=1e-4)
        assert out[0].dimension.end.point[2] == pytest.approx(z)
    # Halfway along the cone is not a radius it shows at an end.
    assert with_dimensions([param("peg_H1_radius", 1.25)], sites)[0].dimension is None


def test_an_arc_is_measured_to_its_middle() -> None:
    out = with_dimensions([param("r", 5.0, feature="lid")], sites_of(BOSSES))
    dim = out[0].dimension
    assert dim is not None and dim.end.face == "lid.corner"
    angle = math.atan2(dim.end.point[1] - 15, dim.end.point[0] - 15)
    assert 0 < angle < math.pi / 2


def test_a_diameter_spans_a_whole_circle_through_its_centre() -> None:
    out = with_dimensions([param("hole_diameter", 4.5)], sites_of(BOSSES))
    dim = out[0].dimension
    assert dim is not None and dim.kind == "distance"
    assert dim.start.face == dim.end.face == "lid.hole_1.0"
    assert math.dist(dim.start.point, dim.end.point) == pytest.approx(4.5, abs=1e-4)
    # An arc has no far side to measure a diameter to.
    assert with_dimensions([param("lid_diameter", 10.0)], sites_of(BOSSES))[0].dimension is None


def test_a_radius_of_another_size_or_feature_is_not_matched() -> None:
    sites = sites_of(BOSSES)
    for p in (
        param("boss_1_radius", 4.6),
        param("screw_radius", 4.5),  # no such feature
        param("radius", 4.5),  # no feature at all
        param("corner_radius", 5.0),  # `corner` names no feature
    ):
        assert with_dimensions([p], sites)[0].dimension is None, p.name


# --- What stays null ----------------------------------------------------------------------------


def test_values_with_no_single_place_on_the_model_keep_no_dimension() -> None:
    params = [
        param("draft", 1.5, unit="deg"),
        param("clearance", 0.5),
        param("material", "molded ABS", unit=None),
        param("z_top", 7.6),
        param("lean", 60.0, unit=None),  # an angle by its name, though it matches a size
        param("negative", -60.0),
    ]
    out = with_dimensions(params, sites_of(BOX))
    assert [p.dimension for p in out] == [None] * len(params)
    assert [p.unit for p in out] == [p.unit for p in params]


def test_a_bare_number_that_matches_becomes_a_length_in_mm() -> None:
    out = with_dimensions([param("length", 60, unit=None)], sites_of(BOX))
    assert out[0].unit == "mm" and out[0].dimension is not None
    Parameter.model_validate(out[0].model_dump())  # still a valid parameter


def test_without_a_model_nothing_is_measured() -> None:
    params = [param("length", 60.0)]
    assert measured(params, None) == params


def test_a_given_dimension_stays_while_its_faces_do_and_goes_with_them() -> None:
    given = Dimension(
        kind="distance",
        start=DimensionEnd(point=(1, 0, 0), part="box", face="box.zlo"),
        end=DimensionEnd(point=(1, 0, 30), part="box", face="box.zhi"),
    )
    drag = Drag(faces=["box.zhi"], axis=(0.0, 0.0, 1.0))
    kept = with_dimensions([param("tall", 30.0, dimension=given, drag=drag)], sites_of(BOX))
    assert kept[0].dimension == given and kept[0].drag == drag

    stale = given.model_copy(update={"end": given.end.model_copy(update={"face": "box.gone"})})
    gone_drag = Drag(faces=["box.gone"], axis=(0.0, 0.0, 1.0))
    out = with_dimensions([param("width", 40.0, dimension=stale, drag=gone_drag)], sites_of(BOX))
    assert out[0].drag is None
    assert out[0].dimension is not None and out[0].dimension.end.face == "box.yhi"


def test_grouped_values_that_measure_join_the_list_and_the_rest_stay_out() -> None:
    design = {"wall": 2.5, "boss": {"radius": 4.5, "bottom": 0.6}, "bosses": [{"x": 1.0}]}
    assert [g.name for g in grouped_values(design)] == ["boss.radius", "boss.bottom"]

    out = with_dimensions([param("wall", 2.5), *grouped_values(design)], sites_of(BOSSES))
    assert [p.name for p in out if p.dimension] == ["boss.radius"]


# --- The demos, on the real API and the mock ----------------------------------------------------


@pytest.fixture(params=["real", "mock"])
def client(request, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MOCK_API_SPEED", "0")
    if request.param == "real":
        from app.main import app
        from app.session_api import CONFIG, SESSIONS, fixture_services

        monkeypatch.setattr(CONFIG, "services", fixture_services)
        SESSIONS.by_id.clear()
    else:
        from app.mock_api import SESSIONS, app

        SESSIONS.clear()
    with TestClient(app) as c:
        c.api = request.param  # type: ignore[attr-defined]
        yield c


# What each demo measures: parameter -> (kind, length). Read from the demos' own models.
DEMOS = {
    # The demos are recorded runs (`demos/README.md`): the sizes are those Claude chose.
    "sample-l-bracket": {
        "leg_a_length": ("distance", 40.0),
        "leg_b_length": ("distance", 40.0),
        "width": ("distance", 20.0),
    },
    "sample-open-box": {
        "length": ("distance", 60.0),
        "width": ("distance", 40.0),
        "height": ("distance", 30.0),
    },
    "sample-phone-stand": {"width": ("distance", 80.0), "base_depth": ("distance", 95.0)},
    "sample-b-parasite-case-on-v1-2": {"boss.radius": ("radius", 4.5)},
    "sample-b-parasite-v2-0-arrives": {"boss.radius": ("radius", 4.5)},
    "sample-b-parasite-case-updated-for-v2-0": {"boss.radius": ("radius", 4.5)},
}


def _measured(listed: list[dict]) -> dict[str, tuple[str, float]]:
    out = {}
    for p in listed:
        dim = p.get("dimension")
        if dim:
            assert p["unit"] == "mm"
            out[p["name"]] = (
                dim["kind"],
                round(math.dist(dim["start"]["point"], dim["end"]["point"]), 3),
            )
            for end in (dim["start"], dim["end"]):
                assert (end["face"] is None) == (end["part"] is None)
    return out


@pytest.mark.parametrize("sid", sorted(DEMOS))
def test_every_demo_measures_its_sizes_on_the_model(client, sid) -> None:
    """Regression: no parameter of any demo had a dimension, so the Dimensions view was empty."""
    original = client.get(f"/api/sessions/{sid}/parameters").json()
    assert _measured(original) == DEMOS[sid]

    copy = client.post(f"/api/demos/{sid}/session").json()
    listed = client.get(f"/api/sessions/{copy['id']}/parameters").json()
    assert _measured(listed) == DEMOS[sid]


def test_a_copy_measures_after_a_restart_and_a_grouped_value_is_editable(client) -> None:
    sid = "sample-b-parasite-case-on-v1-2"
    copy = client.post(f"/api/demos/{sid}/session").json()
    base = f"/api/sessions/{copy['id']}"
    if client.api == "real":  # type: ignore[attr-defined]
        from app.session_api import SESSIONS

        SESSIONS.by_id.clear()  # a restart: the session comes back from the session store
    assert _measured(client.get(base + "/parameters").json()) == DEMOS[sid]

    edited = client.patch(base + "/parameters/boss.radius", json={"value": 4.8})
    assert edited.status_code == 200, edited.text
    assert edited.json()["value"] == 4.8 and edited.json()["locked"] is True
    listed = by_name([Parameter.model_validate(p) for p in client.get(base + "/parameters").json()])
    assert listed["boss.radius"].value == 4.8 and listed["boss.radius"].locked
    if client.api == "real":  # type: ignore[attr-defined]
        from app.session_api import SESSIONS

        assert SESSIONS.get(copy["id"]).ws.design.params["boss"]["radius"] == 4.8


def test_the_demo_original_stays_read_only(client) -> None:
    response = client.patch(
        "/api/sessions/sample-b-parasite-case-on-v1-2/parameters/boss.radius", json={"value": 5}
    )
    assert response.status_code == 409


def test_an_edit_answers_with_the_parameter_as_measured(client) -> None:
    """The canvas puts the edit's answer in its list: without the dimension, the line went."""
    copy = client.post("/api/demos/sample-l-bracket/session").json()
    edited = client.patch(
        f"/api/sessions/{copy['id']}/parameters/leg_a_length", json={"locked": True}
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["unit"] == "mm" and edited.json()["dimension"]["kind"] == "distance"
