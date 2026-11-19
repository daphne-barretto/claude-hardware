"""Renders the synthetic photos of the photo-to-part evaluation set from `photos.yaml`.

Each photo is a plain object on a table, drawn in perspective with soft light, a shadow and
sensor noise: sample data, no people, no real photos. The true dimensions are in `photos.yaml`;
the drawing is to scale in pixels per mm, so the object's proportions are right, but nothing in
the picture gives the absolute size away.

    cd backend && .venv/bin/python ../evals/photos/render.py
"""

from __future__ import annotations

import random
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).parent
SIZE = (960, 720)
HORIZON = 330  # where the table's back edge meets the wall, in pixels


def gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(size[1]):
        t = y / max(size[1] - 1, 1)
        draw.line(
            [(0, y), (size[0], y)],
            fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)),
        )
    return image


def shade(color: tuple, k: float) -> tuple:
    return tuple(max(0, min(255, round(c * k))) for c in color)


def scene() -> Image.Image:
    image = gradient(SIZE, (214, 210, 202), (188, 183, 174))
    table = gradient((SIZE[0], SIZE[1] - HORIZON), (156, 120, 88), (118, 86, 60))
    image.paste(table, (0, HORIZON))
    return image


def shadow(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    layer = Image.new("L", SIZE, 0)
    ImageDraw.Draw(layer).ellipse(box, fill=120)
    layer = layer.filter(ImageFilter.GaussianBlur(14))
    dark = Image.new("RGB", SIZE, (60, 45, 32))
    return Image.composite(dark, image, layer)


def cylinder(
    image: Image.Image, cx: int, base: int, w: int, h: int, color: tuple, handle: bool
) -> Image.Image:
    ry = max(w // 7, 6)  # the ellipse of the rim, seen from a little above
    image = shadow(image, (cx - w // 2 - 10, base - ry, cx + w // 2 + 60, base + ry + 8))
    draw = ImageDraw.Draw(image)
    top = base - h
    if handle:
        hw, hh = w * 0.32, h * 0.55
        box = (cx + w // 2 - hw * 0.4, top + h * 0.2, cx + w // 2 + hw, top + h * 0.2 + hh)
        draw.ellipse(box, outline=shade(color, 0.8), width=max(w // 12, 6))
    draw.ellipse((cx - w // 2, base - ry, cx + w // 2, base + ry), fill=shade(color, 0.7))
    for x in range(-w // 2, w // 2 + 1):  # lit from the left
        k = 1.15 - 0.55 * max(0.0, (x + w / 2) / w) ** 1.4
        draw.line([(cx + x, top), (cx + x, base)], fill=shade(color, k))
    draw.ellipse((cx - w // 2, top - ry, cx + w // 2, top + ry), fill=shade(color, 1.05))
    inset = max(w // 16, 4)
    draw.ellipse(
        (cx - w // 2 + inset, top - ry + inset // 2, cx + w // 2 - inset, top + ry - inset // 2),
        fill=shade(color, 0.45),
    )
    return image


def box(
    image: Image.Image, cx: int, base: int, w: int, d: int, h: int, color: tuple
) -> Image.Image:
    # Seen from the front and a little to the right and above: front face, right side, top.
    dx, dy = round(d * 0.55), round(d * 0.35)
    image = shadow(
        image, (cx - w // 2 - 6, base - dy // 2, cx + w // 2 + dx + 50, base + dy // 2 + 10)
    )
    draw = ImageDraw.Draw(image)
    left, right, top = cx - w // 2, cx + w // 2, base - h
    draw.polygon(
        [(right, top), (right + dx, top - dy), (right + dx, base - dy), (right, base)],
        fill=shade(color, 0.7),
    )
    draw.polygon(
        [(left, top), (left + dx, top - dy), (right + dx, top - dy), (right, top)],
        fill=shade(color, 1.12),
    )
    draw.rectangle((left, top, right, base), fill=color)
    return image


def noisy(image: Image.Image, seed: int) -> Image.Image:
    rng = random.Random(seed)
    noise = Image.effect_noise(SIZE, 18).convert("RGB")
    image = Image.blend(image, noise, 0.06 + rng.random() * 0.02)
    return image.filter(ImageFilter.GaussianBlur(0.8))


def render(entry: dict, seed: int) -> Image.Image:
    true = entry["size"]
    scale = entry["px_per_mm"]
    w, d, h = (round(true[k] * scale) for k in ("width", "depth", "height"))
    image = scene()
    cx, base = SIZE[0] // 2 - 20, HORIZON + 250
    color = tuple(entry["color"])
    if true["shape"] == "cylinder":
        image = cylinder(image, cx, base, w, h, color, entry.get("handle", False))
    else:
        image = box(image, cx, base, w, d, h, color)
    return noisy(image, seed)


def main() -> None:
    entries = yaml.safe_load((HERE / "photos.yaml").read_text())["photos"]
    for n, entry in enumerate(entries):
        render(entry, n).save(HERE / entry["file"], "JPEG", quality=88)
        print("wrote", entry["file"])


if __name__ == "__main__":
    main()
