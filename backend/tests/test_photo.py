"""Photo to part, without a model: the photo reaches Claude with the message through the
Agent SDK, the object estimate is listed in chat, one real measurement rescales and rebuilds,
and the evaluation set is complete. `test_photo_live.py` runs the set on the real model.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os

import pytest
from PIL import Image
from test_agent import run
from test_agent import workspace as agent_workspace
from test_photo_live import (
    PHOTOS_DIR,
    photo_file,
    photo_set,
    score,
    score_lines,
)
from test_session_api import claude, client, send, start, wait_until  # noqa: F401

from app.agent import run as agent_run
from app.agent.photo import MAX_IMAGE_BYTES, MAX_IMAGE_EDGE, Prompt, fit_image, user_input
from app.agent.run import run_agent
from app.agent.scripted import ScriptedDriver
from app.agent.tools import AttachedFile
from app.session_api import SESSIONS


def workspace():
    """A workspace on instant fake builds whose checks pass."""
    return agent_workspace(fixed=lambda params: True)


def png(size=(40, 30), color=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def photo(name="mug.png", data=None) -> AttachedFile:
    return AttachedFile(name, "image/png", data or png())


def texts(ws) -> str:
    return "".join(e.delta for e in ws.sink.of_type("text"))


ESTIMATE = {
    "object": "a mug",
    "photo": "mug.png",
    "shape": "cylinder",
    "width": 80,
    "depth": 80,
    "height": 100,
    "confidence": {"width": "medium", "depth": "medium", "height": "low"},
    "scale_source": "assumed",
    "scale_basis": "a standard coffee mug is about 100 mm tall",
    "assumptions": ["the handle sticks out 30 mm and the stand leaves it free"],
}
STAND = "def build(params):\n    return []\n"


def stand_params(ws) -> dict:
    return {**ws.design.params, "object": {"width": 80, "depth": 80, "height": 100}}


# --- The photo goes to Claude with the message ------------------------------------------------


def test_a_photo_attached_to_the_message_goes_to_claude_as_an_image_block():
    ws = workspace()
    data = png()
    ws.attach([photo(data=data), AttachedFile("notes.txt", "text/plain", b"hello")])
    _, driver = run(ws, [("text", "A mug.")], prompt="Make a stand for this mug.")
    prompt = driver.prompt
    assert isinstance(prompt, Prompt) and prompt == "Make a stand for this mug."
    content = prompt.content()
    images = [b for b in content if b["type"] == "image"]
    assert [i["source"]["media_type"] for i in images] == ["image/png"]
    assert base64.b64decode(images[0]["source"]["data"]) == data
    assert content[-1] == {"type": "text", "text": "Make a stand for this mug."}
    assert {"type": "text", "text": "Attached photo mug.png:"} in content


def test_a_message_without_a_photo_stays_plain_text():
    ws = workspace()
    ws.attach([AttachedFile("notes.txt", "text/plain", b"hello")])
    _, driver = run(ws, [("text", "Ok.")], prompt="Read my notes.")
    assert not getattr(driver.prompt, "images", ())
    assert user_input(driver.prompt) == "Read my notes."


def test_the_sdk_driver_sends_the_photo_in_the_user_turn(monkeypatch):
    """The real driver, on a fake SDK client: the photo is in the user message it queries."""
    from claude_agent_sdk import ResultMessage

    sent: list = []

    class FakeClient:
        def __init__(self, options):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt):
            if isinstance(prompt, str):
                sent.append(prompt)
            else:
                sent.extend([m async for m in prompt])

        async def receive_response(self):
            yield ResultMessage(
                subtype="success",
                duration_ms=0,
                duration_api_ms=0,
                is_error=False,
                num_turns=1,
                session_id="fake",
            )

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    ws = workspace()
    ws.attach([photo()])
    result = asyncio.run(run_agent(ws, "Make a stand.", driver=agent_run.sdk_driver))
    assert result.error is None
    (message,) = sent
    assert message["type"] == "user" and message["message"]["role"] == "user"
    kinds = [b["type"] for b in message["message"]["content"]]
    assert kinds == ["text", "image", "text"]
    assert message["message"]["content"][-1]["text"] == "Make a stand."


def test_a_big_phone_photo_is_scaled_down_to_fit_the_api():
    big = Image.frombytes("RGB", (1500, 1400), os.urandom(1500 * 1400 * 3))
    out = io.BytesIO()
    big.save(out, "PNG")
    assert out.tell() > MAX_IMAGE_BYTES
    data, media_type = fit_image(out.getvalue(), "image/png")
    assert media_type == "image/jpeg" and len(data) <= MAX_IMAGE_BYTES
    assert max(Image.open(io.BytesIO(data)).size) <= MAX_IMAGE_EDGE
    wide, _ = fit_image(png((3000, 1000)), "image/png")
    assert Image.open(io.BytesIO(wide)).size == (MAX_IMAGE_EDGE, 523)
    small = png()
    assert fit_image(small, "image/png") == (small, "image/png")


def test_a_photo_taken_sideways_is_turned_upright():
    image = Image.new("RGB", (60, 20), (10, 200, 10))
    exif = image.getexif()
    exif[0x0112] = 6  # rotate 90 degrees to view
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif)
    data, _ = fit_image(out.getvalue(), "image/jpeg")
    assert Image.open(io.BytesIO(data)).size == (20, 60)


def test_read_attachment_scales_down_a_big_photo_instead_of_refusing_it():
    ws = workspace()
    ws.attach([photo(data=png((4000, 3000)))])
    result = asyncio.run(ws.read_attachment({"name": "mug.png"}))
    assert not result.get("is_error")
    image = result["content"][1]
    assert image["mimeType"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(base64.b64decode(image["data"]))).size) == MAX_IMAGE_EDGE


def test_an_unreadable_photo_is_named_not_sent():
    prompt = Prompt("Make a stand.", [("broken.png", "image/png", b"not a png")])
    content = prompt.content()
    assert [b["type"] for b in content] == ["text", "text"]
    assert content[0]["text"].startswith("Attached photo broken.png: broken.png is not a readable")


# --- The object estimate and the assumed scale ------------------------------------------------


def test_the_estimate_and_its_assumptions_are_listed_in_chat_with_the_assumed_scale():
    ws = workspace()
    ws.attach([photo()])
    result, driver = run(
        ws,
        [
            ("estimate_object", ESTIMATE),
            ("write_design", {"code": STAND, "params": stand_params(ws)}),
            ("finish", {"outcome": "done", "summary": "A stand for the mug."}),
        ],
        prompt="Make a stand for this mug.",
    )
    estimate = driver.calls[0]
    assert not estimate.is_error, estimate.text
    said = texts(ws)
    assert "a mug, cylinder, about 80 × 80 × 100 mm (W × D × H)" in said
    assert "Confidence: width medium, depth medium, height low" in said
    assert "Scale assumed: a standard coffee mug is about 100 mm tall" in said
    assert "Give me one real measurement" in said
    assert "Assumed: the handle sticks out 30 mm" in said
    assert "ask for one real measurement" in estimate.text
    assert ws.object is not None and ws.object.assumed
    # The build went through the checks (the hook reran them after the edit).
    assert ws.builds and ws.report is not None
    assert result.finished["outcome"] == "done"
    spec = asyncio.run(ws.read_spec({}))["content"][0]["text"]
    assert '"scale": "assumed: a standard coffee mug is about 100 mm tall"' in spec


def test_a_dimension_given_with_the_photo_is_the_measured_scale():
    ws = workspace()
    args = {**ESTIMATE, "height": 97, "scale_source": "measured"}
    args["measured"] = {"dimension": "height", "value": 95}
    result = asyncio.run(ws.estimate_object(args))
    assert not result.get("is_error"), result
    assert ws.object.height == 95 and ws.object.confidence["height"] == "high"
    assert ws.object.scale == "measured: height = 95 mm" and not ws.object.assumed
    said = texts(ws)
    assert "Scale measured: height = 95 mm" in said and "Give me one real" not in said


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"scale_source": None}, "scale_source must be"),
        ({"scale_basis": ""}, "needs scale_basis"),
        ({"scale_source": "measured"}, "needs measured.dimension"),
        ({"width": -3}, "width"),
        ({"shape": "blob"}, "shape"),
    ],
)
def test_a_bad_estimate_is_refused_with_what_is_wrong(change, error):
    ws = workspace()
    result = asyncio.run(ws.estimate_object({**ESTIMATE, **change}))
    assert result.get("is_error") and error in result["content"][0]["text"]
    assert ws.object is None


# --- One real measurement rescales and rebuilds -----------------------------------------------


def test_one_real_dimension_rescales_the_design_and_rebuilds():
    ws = workspace()
    ws.attach([photo()])
    run(
        ws,
        [
            ("estimate_object", ESTIMATE),
            ("write_design", {"code": STAND, "params": stand_params(ws)}),
            ("finish", {"outcome": "done", "summary": "A stand."}),
        ],
    )
    built = len(ws.builds)
    # The next message, as the session API starts its run.
    ws.attach([])
    ws.rounds, ws.finished, ws.base, ws.edits = 0, None, None, []
    result, driver = run(
        ws,
        [
            ("set_object_dimension", {"dimension": "height", "value": 120}),
            ("finish", {"outcome": "done", "summary": "Rescaled."}),
        ],
        prompt="It is 120 mm tall.",
    )
    call = driver.calls[0]
    assert not call.is_error, call.text
    assert ws.design.params["object"] == {"width": 96.0, "depth": 96.0, "height": 120.0}
    assert ws.object.scale == "measured: height = 120 mm"
    assert len(ws.builds) == built + 1 and ws.report is not None
    assert ws.sink.of_type("build_ready")
    assert ws.rounds == 1
    assert "now about 96 × 96 × 120 mm" in texts(ws)
    assert result.finished["outcome"] == "done" and result.proposal is not None


def test_a_measurement_after_a_restart_uses_the_designs_object_parameters():
    ws = workspace()
    ws.design.apply({**ws.design.params, "object": {"width": 50, "depth": 40, "height": 20}}, None)
    result = asyncio.run(ws.set_object_dimension({"dimension": "width", "value": 100}))
    assert not result.get("is_error"), result
    assert ws.design.params["object"] == {"width": 100.0, "depth": 80.0, "height": 40.0}


def test_a_measurement_without_object_parameters_says_how_to_resize():
    ws = workspace()
    asyncio.run(ws.estimate_object(ESTIMATE))
    result = asyncio.run(ws.set_object_dimension({"dimension": "width", "value": 40}))
    assert not result.get("is_error")
    assert "nothing was rebuilt" in result["content"][0]["text"]
    assert ws.object.height == 50 and not ws.builds and ws.rounds == 0


def test_a_measurement_needs_an_estimate_first():
    ws = workspace()
    result = asyncio.run(ws.set_object_dimension({"dimension": "height", "value": 10}))
    assert result.get("is_error") and "estimate_object first" in result["content"][0]["text"]


# --- Through the session API ------------------------------------------------------------------


def test_a_photo_sent_in_the_chat_reaches_claude_with_the_message(client, claude) -> None:  # noqa: F811
    sid, _ = start(client)
    data = png()
    r = client.post(
        f"/api/sessions/{sid}/attachments", files={"file": ("mug.png", data, "image/png")}
    )
    assert r.status_code == 201, r.text
    claude.scripts.append([("finish", {"outcome": "blocked", "summary": "x"})])
    send(client, sid, text="Make a stand for this mug", attachments=[r.json()])
    wait_until(client, sid, "idle")
    (prompt,) = claude.prompts
    assert "- mug.png (image/png): a photo, shown with this message" in prompt
    sent = claude.drivers[0].prompt
    assert isinstance(sent, Prompt) and [i[0] for i in sent.images] == ["mug.png"]
    assert sent.images[0][2] == data
    assert SESSIONS.get(sid).ws.sent[0].name == "mug.png"


# --- The evaluation set -----------------------------------------------------------------------


def test_the_evaluation_set_has_three_photos_with_their_true_dimensions():
    entries = photo_set()
    assert len(entries) == 3
    assert len({e["id"] for e in entries}) == 3
    for entry in entries:
        file = photo_file(entry)
        assert file.is_image, entry["file"]
        Image.open(io.BytesIO(file.data)).verify()
        size = entry["size"]
        assert size["shape"] in ("box", "cylinder")
        assert all(size[d] > 0 for d in ("width", "depth", "height"))
        assert entry["request"].strip()
    # At least one photo comes with a real dimension, and at least one without.
    assert any(e["measurement"] for e in entries) and any(not e["measurement"] for e in entries)
    assert (PHOTOS_DIR / "render.py").exists()


def test_each_photo_is_scored_on_whether_it_built_and_the_checks_ran():
    entry = photo_set()[0]
    ws = workspace()
    ws.attach([photo_file(entry)])
    result, _ = run(
        ws,
        [
            ("estimate_object", {**ESTIMATE, "photo": entry["file"]}),
            ("write_design", {"code": STAND, "params": stand_params(ws)}),
            ("finish", {"outcome": "done", "summary": "A stand."}),
        ],
        prompt=entry["request"],
    )
    assert score(ws, result) == {"built": True, "checked": True, "estimated": True, "error": None}
    unbuilt = workspace()
    nothing = run(unbuilt, [("text", "Hm.")])[0]
    assert score(unbuilt, nothing)["built"] is False
    scores = {"mug-stand": score(ws, result), "tin-cover": score(unbuilt, nothing)}
    assert score_lines(scores) == [
        "mug-stand: built=True checked=True estimated=True",
        "tin-cover: built=False checked=False estimated=False",
    ]


def test_the_scripted_driver_records_the_prompt():
    ws = workspace()
    driver = ScriptedDriver(ws, [])
    asyncio.run(run_agent(ws, "hi", driver=driver))
    assert driver.prompt == "hi"
