from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

_KEYWORD_SEPARATOR = re.compile(r"[,\uFF0C\u3001;\uFF1B\r\n]+")
_JOINER = re.compile(r"[\s\-\u2010-\u2015\u2212]+")
_LATIN_OR_DIGIT = re.compile(r"[a-z0-9]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def normalize_keyword(value: str) -> str:
    """Normalize a keyword without splitting meaningful ordinary spaces."""

    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def parse_keywords(value: str | Iterable[str]) -> list[str]:
    """Parse user-entered keywords from common Chinese and Latin separators.

    A normal space is part of a phrase, so ``air conditioner`` remains one
    keyword.  Deduplication is Unicode-normalized and case-insensitive while
    preserving the first spelling for display.
    """

    raw_values = [value] if isinstance(value, str) else value
    clean: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        if not isinstance(raw, str):
            continue
        for item in _KEYWORD_SEPARATOR.split(raw):
            normalized = normalize_keyword(item)
            key = normalized.casefold()
            if normalized and key not in seen:
                seen.add(key)
                clean.append(normalized)
    return clean


def _searchable_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _JOINER.sub(" ", normalized)


def _keyword_pattern(keyword: str) -> re.Pattern[str] | None:
    normalized = _searchable_text(keyword).strip()
    if not normalized:
        return None
    literal = re.escape(normalized).replace(r"\ ", r"\s+")
    if _HAN.search(normalized) or not _LATIN_OR_DIGIT.search(normalized):
        return re.compile(literal)
    return re.compile(rf"(?<![a-z0-9]){literal}(?![a-z0-9])")


@dataclass(frozen=True, slots=True)
class KeywordMatch:
    """Structured lexical-match evidence for a news article."""

    matched: bool
    matched_keywords: tuple[str, ...]
    matched_fields: tuple[str, ...]
    keyword_fields: Mapping[str, tuple[str, ...]]
    excluded_keywords: tuple[str, ...] = ()
    excluded_fields: tuple[str, ...] = ()


def _collect_matches(
    fields: Mapping[str, str], keywords: Iterable[str]
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, tuple[str, ...]]]:
    normalized_fields = {
        name: _searchable_text(value) for name, value in fields.items() if isinstance(value, str)
    }
    keyword_hits: list[str] = []
    field_hits: list[str] = []
    keyword_fields: dict[str, tuple[str, ...]] = {}
    for keyword in parse_keywords(keywords):
        pattern = _keyword_pattern(keyword)
        if pattern is None:
            continue
        hits = tuple(name for name, text in normalized_fields.items() if pattern.search(text))
        if not hits:
            continue
        keyword_hits.append(keyword)
        keyword_fields[keyword] = hits
        for name in hits:
            if name not in field_hits:
                field_hits.append(name)
    return tuple(keyword_hits), tuple(field_hits), keyword_fields


def match_text_fields(
    fields: Mapping[str, str],
    include_keywords: str | Iterable[str],
    exclude_keywords: str | Iterable[str] = (),
) -> KeywordMatch:
    """Match include/exclude keywords and retain explainable field evidence."""

    includes, include_fields, keyword_fields = _collect_matches(
        fields, parse_keywords(include_keywords)
    )
    excludes, exclude_fields, _ = _collect_matches(fields, parse_keywords(exclude_keywords))
    return KeywordMatch(
        matched=bool(includes) and not excludes,
        matched_keywords=includes,
        matched_fields=include_fields,
        keyword_fields=keyword_fields,
        excluded_keywords=excludes,
        excluded_fields=exclude_fields,
    )


def match_article(
    article: object,
    include_keywords: str | Iterable[str],
    exclude_keywords: str | Iterable[str] = (),
) -> KeywordMatch:
    """Match a FeedArticle-like object across title, excerpt, and full text."""

    return match_text_fields(
        {
            "title": str(getattr(article, "title", "") or ""),
            "excerpt": str(getattr(article, "excerpt", "") or ""),
            "full_text": str(getattr(article, "full_text", "") or ""),
        },
        include_keywords,
        exclude_keywords,
    )
