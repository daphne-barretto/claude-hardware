"""Photos as Claude sees them, and the photographed object as data.

A photo attached to a message goes to Claude with the message itself, as an image block in the
user turn the Claude Agent SDK sends (`Prompt`, `user_input`), so Claude sees it without
having to ask for it; `read_attachment` shows it again later. A photo too big for the API
(over `MAX_IMAGE_BYTES`, or longer than `MAX_IMAGE_EDGE` on a side) is scaled down first.

Claude's estimate of the photographed object is data (`ObjectEstimate`): its shape, width,
depth and height in mm, a confidence for each, and where the scale came from (assumed, or one
real measurement). `estimate_object` records it and lists the assumptions in chat;
`set_object_dimension` takes one real measurement and scales the rest in proportion.
"""

from __future__ import annotations

import base64
import io
from collections.abc import AsyncIterator, Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # the API's limit per image
MAX_IMAGE_EDGE = 1568  # longer images are scaled down by the API anyway
DIMENSIONS = ("width", "depth", "height")

Confidence = Literal["low", "medium", "high"]

# Added to the agent's system prompt (`app.agent.run`).
PHOTO_PROMPT = """
Photos of objects:
- A photo attached to the message comes with it (read_attachment shows it again). When the \
person asks for a part around a photographed object (a stand, a cover, a mount, a holder), \
first estimate the object from the photo and record it with estimate_object: what it is, its \
shape, its width, depth and height in mm with a confidence for each, and the scale. If the \
message gives a real dimension, the scale is "measured": use it exactly and size the rest in \
proportion. Otherwise the scale is "assumed": say what you based it on (a familiar object, a \
standard size), and ask in your reply for one real measurement to correct it, but go ahead \
and build. estimate_object lists the estimate and the assumptions in the chat.
- Put the object in the design's parameters as "object": {"shape": "box" or "cylinder", \
"width": ..., "depth": ..., "height": ... (mm), "at": [x, y, z], the centre of its bottom face \
in the part's coordinates}, and size and place the part from those values in the code, so a \
measurement rescales the part. The object_fit constraint checks the part against that object: \
no part may take up any of its volume, and the closest part is within the constraint's gap.
- When the person gives a real measurement later, call set_object_dimension with it: it \
scales the object in proportion, updates params "object", and rebuilds and rechecks. Then fix \
anything else the new size affects.
- Build and check the part like any design.
"""


def fit_image(data: bytes, media_type: str) -> tuple[bytes, str]:
    """The image as Claude gets it: as it is when it fits, else scaled down to fit (and turned
    upright by its EXIF orientation, as a phone photo needs). Raises ValueError for data that
    isn't an image."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    from app.security.settings import whole

    limit = whole("MAX_IMAGE_PIXELS")
    try:
        image = Image.open(io.BytesIO(data))
        size = image.size
        rotated = image.getexif().get(0x0112, 1) not in (1, None)
    except Image.DecompressionBombError as error:
        raise ValueError(f"too large an image to read ({error})") from error
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError(f"not a readable image: {error}") from error
    # A small file can unpack to a huge image (a decompression bomb): refuse it unread.
    if size[0] * size[1] > limit:
        raise ValueError(
            f"too large an image: {size[0]} x {size[1]} pixels, over the {limit} this app reads"
        )
    if len(data) <= MAX_IMAGE_BYTES and max(size) <= MAX_IMAGE_EDGE and not rotated:
        return data, media_type
    image = ImageOps.exif_transpose(image)
    image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    for quality in (85, 70, 50):
        out = io.BytesIO()
        image.save(out, "JPEG", quality=quality)
        if out.tell() <= MAX_IMAGE_BYTES:
            break
    return out.getvalue(), "image/jpeg"


def image_block(name: str, data: bytes, media_type: str) -> dict:
    """An API image content block for a photo (scaled down if it has to be)."""
    try:
        data, media_type = fit_image(data, media_type)
    except ValueError as error:
        raise ValueError(f"{name} is {error}") from error
    source = {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()}
    return {"type": "image", "source": source}


class Prompt(str):
    """A message's text as Claude gets it, with the photos sent along with it.

    It is the text (every driver can treat it as a string); `images` are (name, media type,
    data) for the photos attached to the message, and `content()` the user turn's content
    blocks: each photo, named, then the text."""

    images: tuple[tuple[str, str, bytes], ...]

    def __new__(cls, text: str, images: Iterable[tuple[str, str, bytes]] = ()) -> Prompt:
        prompt = super().__new__(cls, text)
        prompt.images = tuple(images)
        return prompt

    def content(self) -> list[dict]:
        blocks: list[dict] = []
        for name, media_type, data in self.images:
            try:
                block = image_block(name, data, media_type)
            except ValueError as error:
                blocks.append({"type": "text", "text": f"Attached photo {name}: {error}."})
                continue
            blocks += [{"type": "text", "text": f"Attached photo {name}:"}, block]
        return [*blocks, {"type": "text", "text": str(self)}]


def user_input(prompt: str) -> str | AsyncIterator[dict[str, Any]]:
    """What `ClaudeSDKClient.query` gets: the text, or, with photos, one user message whose
    content is the photos and the text."""
    if not getattr(prompt, "images", ()):
        return str(prompt)
    assert isinstance(prompt, Prompt)

    async def messages() -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "message": {"role": "user", "content": prompt.content()},
            "parent_tool_use_id": None,
        }

    return messages()


# --- The photographed object ------------------------------------------------------------------


class ObjectEstimate(BaseModel):
    """Claude's estimate of a photographed object, in mm."""

    model_config = ConfigDict(extra="forbid")

    object: str = Field(min_length=1, description="What the object is")
    shape: Literal["box", "cylinder", "other"]
    width: float = Field(gt=0)
    depth: float = Field(gt=0)
    height: float = Field(gt=0)
    confidence: dict[Literal["width", "depth", "height"], Confidence]
    # "assumed: <what the scale is based on>" or "measured: <dimension> = <value> mm"
    scale: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    photo: str | None = None  # the attachment it was read from

    @classmethod
    def from_args(cls, args: dict) -> ObjectEstimate:
        """From estimate_object's arguments; raises ValueError with what is wrong."""
        source = args.get("scale_source")
        if source == "measured":
            measured = args.get("measured") or {}
            dimension, value = measured.get("dimension"), measured.get("value")
            if dimension not in DIMENSIONS or not isinstance(value, int | float) or value <= 0:
                raise ValueError(
                    "a measured scale needs measured.dimension (width, depth or height) and "
                    "measured.value in mm"
                )
            scale = f"measured: {dimension} = {value:g} mm"
        elif source == "assumed":
            basis = str(args.get("scale_basis") or "").strip()
            if not basis:
                raise ValueError("an assumed scale needs scale_basis: what the scale is based on")
            scale = f"assumed: {basis}"
        else:
            raise ValueError("scale_source must be 'assumed' or 'measured'")
        fields = {k: args.get(k) for k in ("object", "shape", *DIMENSIONS, "photo")}
        confidence = args.get("confidence") or {}
        try:
            estimate = cls(
                **fields,
                confidence={d: confidence.get(d, "low") for d in DIMENSIONS},
                scale=scale,
                assumptions=[str(a) for a in args.get("assumptions") or []],
            )
        except ValidationError as error:
            raise ValueError(_errors(error)) from error
        if source == "measured":  # the measured dimension is exactly what the person gave
            estimate = estimate.model_copy(
                update={
                    measured["dimension"]: float(measured["value"]),
                    "confidence": {**estimate.confidence, measured["dimension"]: "high"},
                }
            )
        return estimate

    @property
    def assumed(self) -> bool:
        return self.scale.startswith("assumed")

    def sizes(self) -> dict[str, float]:
        return {d: getattr(self, d) for d in DIMENSIONS}

    def rescaled(self, dimension: str, value: float) -> tuple[ObjectEstimate, float]:
        """The estimate with `dimension` measured as `value` mm and the others scaled in
        proportion, and the scale factor."""
        if dimension not in DIMENSIONS:
            raise ValueError("dimension must be width, depth or height")
        if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
            raise ValueError("value must be a length in mm, more than 0")
        factor = value / getattr(self, dimension)
        sizes = {d: round(v * factor, 2) for d, v in self.sizes().items()}
        sizes[dimension] = float(value)
        estimate = self.model_copy(
            update={
                **sizes,
                "confidence": {**self.confidence, dimension: "high"},
                "scale": f"measured: {dimension} = {value:g} mm",
            }
        )
        return estimate, factor

    def size_text(self) -> str:
        return " × ".join(f"{v:g}" for v in self.sizes().values()) + " mm (W × D × H)"

    def chat_text(self) -> str:
        """The estimate and every assumption, as the chat lists them."""
        source = f" from {self.photo}" if self.photo else ""
        lines = [f"**Object{source}:** {self.object}, {self.shape}, about {self.size_text()}."]
        sure = ", ".join(f"{d} {self.confidence[d]}" for d in DIMENSIONS)
        lines.append(f"- Confidence: {sure}.")
        kind, _, basis = self.scale.partition(": ")
        if kind == "assumed":
            lines.append(
                f"- Scale assumed: {basis}. Give me one real measurement (width, depth or "
                "height) and I'll rescale the part to it."
            )
        else:
            lines.append(f"- Scale measured: {basis}.")
        lines += [f"- Assumed: {a}" for a in self.assumptions]
        return "\n\n" + "\n".join(lines) + "\n\n"


def object_from_params(params: dict) -> ObjectEstimate | None:
    """An estimate rebuilt from the design's `object` parameters (after a restart), if any."""
    found = params.get("object")
    if not isinstance(found, dict) or not all(_length(found.get(d)) for d in DIMENSIONS):
        return None
    return ObjectEstimate(
        object=str(found.get("name") or "the photographed object"),
        shape=found.get("shape") if found.get("shape") in ("box", "cylinder") else "other",
        **{d: float(found[d]) for d in DIMENSIONS},
        confidence=dict.fromkeys(DIMENSIONS, "low"),
        scale="assumed: as the design's object parameters give it",
    )


def object_changes(params: dict, estimate: ObjectEstimate) -> dict[str, float] | None:
    """The parameter edits that size the design's `object` to the estimate, or None when the
    design has no `object` parameters to size."""
    found = params.get("object")
    if not isinstance(found, dict):
        return None
    edits = {f"object.{d}": v for d, v in estimate.sizes().items() if _length(found.get(d))}
    return edits or None


def _length(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


def _errors(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'estimate'}: {e['msg']}" for e in error.errors()
    )
