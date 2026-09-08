"""Generate the deterministic Cookie app icon and compile it to ICNS on macOS."""

from __future__ import annotations

import argparse
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

CANVAS_SIZE = 1024
ICON_SIZES = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}


def rounded_square_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def draw_master_icon() -> Image.Image:
    scale = 3
    working_size = CANVAS_SIZE * scale
    image = Image.new("RGBA", (working_size, working_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = 62 * scale
    radius = 224 * scale
    draw.rounded_rectangle(
        (margin, margin, working_size - margin, working_size - margin),
        radius=radius,
        fill=(255, 253, 248, 255),
        outline=(83, 51, 36, 255),
        width=18 * scale,
    )

    # Soft offset shadow keeps the mark legible at Finder's smallest icon size.
    draw.ellipse(
        (216 * scale, 245 * scale, 838 * scale, 867 * scale),
        fill=(83, 51, 36, 38),
    )

    center_x, center_y = 512 * scale, 504 * scale
    cookie_radius = 298 * scale
    points: list[tuple[float, float]] = []
    radii = [1.00, 0.97, 1.01, 0.96, 1.02, 0.98, 1.00, 0.965, 1.015, 0.98, 1.01, 0.97]
    for index, factor in enumerate(radii):
        angle = -math.pi / 2 + index * math.tau / len(radii)
        radius_at_point = cookie_radius * factor
        points.append(
            (
                center_x + math.cos(angle) * radius_at_point,
                center_y + math.sin(angle) * radius_at_point,
            )
        )
    draw.polygon(points, fill=(226, 164, 91, 255), outline=(83, 51, 36, 255), width=18 * scale)

    draw.arc(
        (268 * scale, 262 * scale, 756 * scale, 750 * scale),
        start=203,
        end=323,
        fill=(255, 208, 137, 255),
        width=26 * scale,
    )

    chips = [
        (395, 338, 42),
        (594, 309, 34),
        (677, 461, 45),
        (490, 506, 37),
        (340, 574, 32),
        (590, 664, 43),
        (714, 639, 26),
        (416, 721, 28),
    ]
    for x, y, chip_radius in chips:
        draw.ellipse(
            (
                (x - chip_radius) * scale,
                (y - chip_radius) * scale,
                (x + chip_radius) * scale,
                (y + chip_radius) * scale,
            ),
            fill=(83, 51, 36, 255),
        )
        highlight_radius = max(5, chip_radius // 5)
        draw.ellipse(
            (
                (x - chip_radius // 2 - highlight_radius) * scale,
                (y - chip_radius // 2 - highlight_radius) * scale,
                (x - chip_radius // 2 + highlight_radius) * scale,
                (y - chip_radius // 2 + highlight_radius) * scale,
            ),
            fill=(132, 83, 56, 255),
        )

    image = image.resize((CANVAS_SIZE, CANVAS_SIZE), Image.Resampling.LANCZOS)
    alpha = rounded_square_mask(CANVAS_SIZE, 224)
    image.putalpha(Image.composite(image.getchannel("A"), Image.new("L", alpha.size, 0), alpha))
    return image


def generate_icon(output: Path, work_dir: Path) -> None:
    output = output.resolve()
    work_dir = work_dir.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    master = draw_master_icon()
    for filename, size in ICON_SIZES.items():
        resized = master.resize((size, size), Image.Resampling.LANCZOS)
        resized.save(work_dir / filename, format="PNG", optimize=True)

    subprocess.run(
        ["iconutil", "-c", "icns", str(work_dir), "-o", str(output)],
        check=True,
    )

    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"iconutil did not create a usable ICNS file: {output}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    generate_icon(arguments.output, arguments.work_dir)
