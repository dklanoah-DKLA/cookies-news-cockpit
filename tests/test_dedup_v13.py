from __future__ import annotations

import pytest

from cookies_news_cockpit import dedup
from cookies_news_cockpit.dedup import DuplicateIndex
from cookies_news_cockpit.models import FeedArticle
from cookies_news_cockpit.pipeline import _content_hash

ORIGINAL_URL = "https://news.example/original"
UPDATED_URLS = (ORIGINAL_URL, "https://other.example/syndication")


@pytest.mark.parametrize("updated_url", UPDATED_URLS)
@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ("欧盟发布行业年度展望预计增长1.5%", "欧盟发布行业年度展望预计增长15%"),
        ("欧盟发布行业年度展望预计增长1.5%", "欧盟发布行业年度展望预计增长1.5"),
        ("欧盟发布行业年度展望预计增长-1.5%", "欧盟发布行业年度展望预计增长1.5%"),
        (
            "New climate policy implementation starts in June",
            "New climate policy implementation starts in July",
        ),
        (
            "New climate policy implementation starts in Jun.",
            "New climate policy implementation starts in Jul.",
        ),
        (
            "New climate policy implementation moved from June to July",
            "New climate policy implementation moved from July to June",
        ),
        ("新一轮行业环保政策将在六月实施", "新一轮行业环保政策将在七月实施"),
        (
            "New climate policy implementation starts tomorrow",
            "New climate policy implementation starts today",
        ),
        (
            "New climate policy implementation starts on 2026-06-01",
            "New climate policy implementation starts on 2026-01-06",
        ),
        (
            "Company annual energy investment reaches 15 million dollars",
            "Company annual energy investment reaches 15 billion dollars",
        ),
        (
            "Company annual energy investment reaches $15 million",
            "Company annual energy investment reaches €15 million",
        ),
        ("新一轮行业环保投入已经达到10亿元", "新一轮行业环保投入已经达到10万元"),
        (
            "Company annual energy capacity reaches 100 MW",
            "Company annual energy capacity reaches 100 GW",
        ),
        ("【草案】新一轮行业环保政策将于下月执行", "【通过】新一轮行业环保政策将于下月执行"),
        (
            "New climate policy implementation scheduled in June",
            "New climate policy implementation effective in June",
        ),
    ],
)
def test_material_updates_survive_same_url_and_cross_source_dedup(
    updated_url: str,
    previous: str,
    current: str,
) -> None:
    index = DuplicateIndex([{"url": ORIGINAL_URL, "title": previous}])
    assert not index.is_duplicate(updated_url, current)


@pytest.mark.parametrize("updated_url", UPDATED_URLS)
@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ("欧盟发布行业年度展望预计增长1.5%", "欧盟发布行业年度展望预计增长１．５０％"),
        (
            "欧盟发布行业年度展望预计增长1.5%",
            "快讯：欧盟公布行业年度展望预计增长 1.50 % - 新浪财经",
        ),
        (
            "New industry investment reaches 1,000 million dollars",
            "New industry investment reaches 1000 million dollars",
        ),
        ("New industry investment grows by 1.50 percent", "New industry investment grows by 1.5%"),
        (
            "New climate policy implementation starts in June",
            "New climate policy implementation starts in Jun.",
        ),
        (
            "Company annual energy capacity reaches 100 MW",
            "Company annual energy capacity reaches 100.0 MW",
        ),
        (
            "New climate policy implementation starts on 2026-06-01",
            "New climate policy implementation starts on 2026/6/1",
        ),
    ],
)
def test_formatting_variants_remain_duplicates(
    updated_url: str,
    previous: str,
    current: str,
) -> None:
    index = DuplicateIndex([{"url": ORIGINAL_URL, "title": previous}])
    assert index.is_duplicate(updated_url, current)


def test_material_signature_also_guards_exact_normalized_title_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = "欧盟发布行业年度展望预计增长1.5%"
    current = "欧盟发布行业年度展望预计增长15%"
    # Reproduce the former lossy-title identity explicitly: the independent
    # material signature must protect both exact-title branches as well.
    monkeypatch.setattr(dedup, "normalize_title", lambda _title: "欧盟发布行业年度展望预计增长15")
    index = DuplicateIndex([{"url": ORIGINAL_URL, "title": previous}])
    assert not index.is_duplicate(ORIGINAL_URL, current)
    assert not index.is_duplicate(UPDATED_URLS[1], current)


def test_short_generic_titles_still_do_not_merge_different_urls() -> None:
    index = DuplicateIndex([{"url": ORIGINAL_URL, "title": "最新行业消息"}])
    assert not index.is_duplicate(UPDATED_URLS[1], "最新行业消息")


def test_numeric_updates_have_distinct_report_hashes_on_the_same_url() -> None:
    def article(title: str) -> FeedArticle:
        return FeedArticle(source_id="source", source_name="Source", title=title, url=ORIGINAL_URL)

    decimal = article("欧盟发布行业年度展望预计增长1.5%")
    integer = article("欧盟发布行业年度展望预计增长15%")
    formatting = article("欧盟发布行业年度展望预计增长１．５０％")
    assert _content_hash(decimal) != _content_hash(integer)
    assert _content_hash(decimal) == _content_hash(formatting)
    assert _content_hash(article("New climate policy starts on 2026-1-11")) != _content_hash(
        article("New climate policy starts on 2026-11-1")
    )
    assert _content_hash(article("【草案】新一轮行业环保政策将于下月执行")) != _content_hash(
        article("【通过】新一轮行业环保政策将于下月执行")
    )
