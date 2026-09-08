from __future__ import annotations

import math
import re
from pathlib import Path

TOKENS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "cookies_news_cockpit"
    / "static"
    / "tokens.css"
)


def _oklch(name: str) -> tuple[float, float, float]:
    css = TOKENS.read_text(encoding="utf-8")
    match = re.search(
        rf"--color-{re.escape(name)}:\s*oklch\(([\d.]+)%\s+([\d.]+)\s+([\d.]+)",
        css,
    )
    assert match, f"Missing direct OKLCH token: {name}"
    lightness, chroma, hue = (float(value) for value in match.groups())
    return lightness / 100, chroma, hue


def _luminance(color: tuple[float, float, float]) -> float:
    lightness, chroma, hue = color
    angle = math.radians(hue)
    axis_a = chroma * math.cos(angle)
    axis_b = chroma * math.sin(angle)
    long = lightness + 0.3963377774 * axis_a + 0.2158037573 * axis_b
    medium = lightness - 0.1055613458 * axis_a - 0.0638541728 * axis_b
    short = lightness - 0.0894841775 * axis_a - 1.291485548 * axis_b
    long, medium, short = long**3, medium**3, short**3
    red = 4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short
    green = -1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short
    blue = -0.0041960863 * long - 0.7034186147 * medium + 1.707614701 * short
    red, green, blue = (max(0.0, min(1.0, value)) for value in (red, green, blue))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground: str, background: str) -> float:
    first = _luminance(_oklch(foreground))
    second = _luminance(_oklch(background))
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def test_text_color_pairs_meet_wcag_aa() -> None:
    pairs = (
        ("ink", "paper"),
        ("ink-soft", "paper"),
        ("muted", "paper"),
        ("faint", "paper"),
        ("accent-ink", "accent"),
        ("accent-deep", "paper"),
        ("berry", "berry-soft"),
        ("leaf", "leaf-soft"),
    )
    failures = {
        f"{foreground}/{background}": round(_contrast(foreground, background), 2)
        for foreground, background in pairs
        if _contrast(foreground, background) < 4.5
    }
    assert not failures, f"Text contrast below 4.5:1: {failures}"


def test_focus_and_control_boundaries_meet_three_to_one() -> None:
    pairs = (
        ("focus", "paper"),
        ("rule-strong", "paper"),
        ("rule-strong", "paper-soft"),
    )
    failures = {
        f"{foreground}/{background}": round(_contrast(foreground, background), 2)
        for foreground, background in pairs
        if _contrast(foreground, background) < 3.0
    }
    assert not failures, f"Non-text contrast below 3:1: {failures}"
