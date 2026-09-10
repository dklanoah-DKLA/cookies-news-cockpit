from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from scripts.audit_source_presets import (
    CATALOG_V2_SOURCE_IDS,
    REPLACEMENTS,
    inspect_feed,
)

from cookies_news_cockpit.presets import PRESET_CATALOG_VERSION, SOURCE_PRESETS

FIXTURES = Path(__file__).parent / "fixtures" / "source_health"


def test_catalog_v2_adds_eight_https_opt_in_rss_sources() -> None:
    selected = [preset for preset in SOURCE_PRESETS if preset.id in CATALOG_V2_SOURCE_IDS]

    assert PRESET_CATALOG_VERSION == 2
    assert len(selected) == 8
    assert {preset.id for preset in selected} == set(CATALOG_V2_SOURCE_IDS)
    assert all(preset.url.startswith("https://") for preset in selected)
    assert all(preset.homepage.startswith("https://") for preset in selected)
    assert all(preset.terms.startswith("https://") for preset in selected)
    assert all(preset.default_enabled is False for preset in selected)
    assert all("rss" in preset.url.lower() or "feed" in preset.url.lower() for preset in selected)


def test_imf_failure_is_transparently_replaced_by_bis() -> None:
    assert REPLACEMENTS == {
        "preset-bis-media-releases": "IMF News/Blog (403 to app client)"
    }


@pytest.mark.parametrize("fixture_name", ["rss.xml", "atom.xml"])
def test_source_health_audit_parses_offline_feed_fixtures(fixture_name: str) -> None:
    result = inspect_feed(
        (FIXTURES / fixture_name).read_bytes(),
        feed_url=f"https://fixture.example/{fixture_name}",
        checked_at=datetime(2026, 9, 10, tzinfo=UTC),
    )

    assert result["ok"] is True
    assert result["entries"] >= 1
    assert result["sample_titles"] == result["sample_size"]
    assert result["sample_http_links"] == result["sample_size"]
    assert result["sample_dates"] == result["sample_size"]
    assert result["latest_published"].startswith("2026-09-09")


@pytest.mark.parametrize(
    ("fixture_name", "content_type", "expected_error"),
    [
        ("stale.xml", "application/rss+xml", "older than 60 days"),
        ("missing-date.xml", "application/rss+xml", "without a published/updated date"),
        ("invalid-format.html", "text/html", "not a recognized RSS/Atom feed"),
    ],
)
def test_source_health_audit_rejects_unhealthy_offline_fixtures(
    fixture_name: str,
    content_type: str,
    expected_error: str,
) -> None:
    result = inspect_feed(
        (FIXTURES / fixture_name).read_bytes(),
        feed_url=f"https://fixture.example/{fixture_name}",
        content_type=content_type,
        checked_at=datetime(2026, 9, 10, tzinfo=UTC),
    )

    assert result["ok"] is False
    assert any(expected_error in error for error in result["errors"])
