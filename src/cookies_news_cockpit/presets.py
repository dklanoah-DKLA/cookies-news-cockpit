from __future__ import annotations

from dataclasses import dataclass

PRESET_CATALOG_VERSION = 1


@dataclass(frozen=True, slots=True)
class SourcePreset:
    id: str
    name: str
    url: str
    homepage: str
    category: str
    language: str
    terms: str
    default_enabled: bool = False


# The catalog deliberately stays industry-neutral.  Feed URLs are the stable
# merge key; display names and descriptive metadata may evolve in later packs.
SOURCE_PRESETS: tuple[SourcePreset, ...] = (
    SourcePreset(
        "preset-chinanews-scroll",
        "中新网 · 即时新闻",
        "https://www.chinanews.com.cn/rss/scroll-news.xml",
        "https://www.chinanews.com.cn/",
        "general",
        "zh",
        "https://www.chinanews.com.cn/common/footer/aboutus.shtml",
        True,
    ),
    SourcePreset(
        "preset-chinanews-world",
        "中新网 · 国际",
        "https://www.chinanews.com.cn/rss/world.xml",
        "https://www.chinanews.com.cn/gj/",
        "world",
        "zh",
        "https://www.chinanews.com.cn/common/footer/aboutus.shtml",
    ),
    SourcePreset(
        "preset-chinanews-finance",
        "中新网 · 财经",
        "https://www.chinanews.com.cn/rss/finance.xml",
        "https://www.chinanews.com.cn/cj/",
        "business",
        "zh",
        "https://www.chinanews.com.cn/common/footer/aboutus.shtml",
    ),
    SourcePreset(
        "preset-solidot",
        "Solidot",
        "https://www.solidot.org/index.rss",
        "https://www.solidot.org/",
        "technology",
        "zh",
        "https://www.solidot.org/",
    ),
    SourcePreset(
        "preset-cna-world",
        "中央社 · 国际",
        "https://feeds.feedburner.com/rsscna/intworld",
        "https://www.cna.com.tw/list/aopl.aspx",
        "world",
        "zh",
        "https://www.cna.com.tw/about/copyright.aspx",
    ),
    SourcePreset(
        "preset-cna-finance",
        "中央社 · 产经证券",
        "https://feeds.feedburner.com/rsscna/finance",
        "https://www.cna.com.tw/list/afe.aspx",
        "business",
        "zh",
        "https://www.cna.com.tw/about/copyright.aspx",
    ),
    SourcePreset(
        "preset-cna-technology",
        "中央社 · 科技",
        "https://feeds.feedburner.com/rsscna/technology",
        "https://www.cna.com.tw/list/ait.aspx",
        "technology",
        "zh",
        "https://www.cna.com.tw/about/copyright.aspx",
    ),
    SourcePreset(
        "preset-un-zh",
        "联合国新闻 · 中文",
        "https://news.un.org/feed/subscribe/zh/news/all/rss.xml",
        "https://news.un.org/zh/",
        "policy",
        "zh",
        "https://www.un.org/zh/about-us/copyright",
        True,
    ),
    SourcePreset(
        "preset-bbc-world",
        "BBC World",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.bbc.com/news/world",
        "world",
        "en",
        "https://www.bbc.com/usingthebbc/terms",
    ),
    SourcePreset(
        "preset-bbc-business",
        "BBC Business",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.bbc.com/news/business",
        "business",
        "en",
        "https://www.bbc.com/usingthebbc/terms",
    ),
    SourcePreset(
        "preset-guardian-world",
        "The Guardian · World",
        "https://www.theguardian.com/world/rss",
        "https://www.theguardian.com/world",
        "world",
        "en",
        "https://www.theguardian.com/help/terms-of-service",
    ),
    SourcePreset(
        "preset-guardian-business",
        "The Guardian · Business",
        "https://www.theguardian.com/uk/business/rss",
        "https://www.theguardian.com/uk/business",
        "business",
        "en",
        "https://www.theguardian.com/help/terms-of-service",
    ),
    SourcePreset(
        "preset-sciencedaily-top",
        "ScienceDaily · Top",
        "https://www.sciencedaily.com/rss/top.xml",
        "https://www.sciencedaily.com/",
        "science",
        "en",
        "https://www.sciencedaily.com/terms.htm",
    ),
    SourcePreset(
        "preset-nsf-news",
        "NSF News",
        "https://www.nsf.gov/rss/rss_www_news.xml",
        "https://www.nsf.gov/news/",
        "science",
        "en",
        "https://www.nsf.gov/policies/",
        True,
    ),
    SourcePreset(
        "preset-wto-news",
        "WTO News",
        "https://www.wto.org/library/rss/latest_news_e.xml",
        "https://www.wto.org/english/news_e/news_e.htm",
        "policy",
        "en",
        "https://www.wto.org/english/info_e/copyright_e.htm",
        True,
    ),
    SourcePreset(
        "preset-un-en",
        "UN News · English",
        "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
        "https://news.un.org/en/",
        "policy",
        "en",
        "https://www.un.org/en/about-us/copyright",
    ),
)


SOURCE_PRESETS_BY_URL = {preset.url: preset for preset in SOURCE_PRESETS}
