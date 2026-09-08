from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MIN_CROSS_URL_TITLE_LENGTH = 12
SAME_URL_SIMILARITY = 0.88
CROSS_URL_SIMILARITY = 0.90
CROSS_URL_CONTAINMENT = 0.78
MAX_FUZZY_CANDIDATES = 200

_TRACKING_PARAMETERS = {
    "fbclid",
    "from",
    "gclid",
    "ref",
    "ref_src",
    "source",
    "spm",
}
_MATERIAL_TERMS = (
    "上调",
    "下调",
    "取消",
    "完成",
    "获批",
    "批准",
    "确认",
    "否认",
    "正式",
    "召回",
    "新增",
    "暂停",
    "恢复",
    "终止",
    "已",
    "拟",
    "将",
    "confirmed",
    "denied",
    "approved",
    "cancelled",
    "canceled",
    "completed",
    "raised",
    "lowered",
)
_BOILERPLATE_FRAGMENTS = {
    "据悉",
    "快讯",
    "报道",
    "最新",
    "最新消息",
    "消息",
    "独家",
}
_OUTLET_SUFFIXES = (
    "新浪财经",
    "新浪新闻",
    "腾讯新闻",
    "网易新闻",
    "搜狐新闻",
    "凤凰网",
    "中国新闻网",
    "中新网",
    "界面新闻",
    "澎湃新闻",
    "第一财经",
    "证券时报",
    "证券日报",
)
_OUTLET_ENDINGS = ("新闻网", "新闻客户端", "财经网", "日报", "时报", "周刊")
_TITLE_EQUIVALENTS = (
    ("推出", "发布"),
    ("公布", "发布"),
    ("推介", "发布"),
    ("表示", "称"),
    ("指出", "称"),
    ("透露", "称"),
)
_LEADING_LABEL_RE = re.compile(r"^\s*(?:【[^】]{1,16}】|\[[^\]]{1,16}\])\s*")
_SEPARATED_SUFFIX_RE = re.compile(r"\s*[-_|｜—–·]\s*([^\s]{2,20})\s*$")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*(?:%|％)?")


def normalize_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold().strip()
    normalized = _LEADING_LABEL_RE.sub("", normalized)
    for fragment in sorted(_BOILERPLATE_FRAGMENTS, key=len, reverse=True):
        if normalized.startswith(fragment):
            normalized = normalized[len(fragment) :].lstrip(" :：|｜-_—–·")
            break
    separated = _SEPARATED_SUFFIX_RE.search(normalized)
    if separated:
        suffix = separated.group(1)
        if suffix in _OUTLET_SUFFIXES or suffix.endswith(_OUTLET_ENDINGS):
            normalized = normalized[: separated.start()]
    for suffix in _OUTLET_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            normalized = normalized[: -len(suffix)].rstrip(" :：|｜-_—–·")
            break
    for source, replacement in _TITLE_EQUIVALENTS:
        normalized = normalized.replace(source, replacement)
    return "".join(character for character in normalized if character.isalnum())


def canonical_url(url: str) -> str:
    raw = unicodedata.normalize("NFKC", url.strip()).casefold()
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").casefold()
        port = parsed.port
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            host = f"{host}:{port}"
        path = parsed.path or "/"
        if path != "/":
            path = path.rstrip("/")
        query = urlencode(
            sorted(
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if not key.casefold().startswith("utm_")
                and key.casefold() not in _TRACKING_PARAMETERS
            ),
            doseq=True,
        )
        return urlunsplit((scheme, host, path, query, ""))
    except (UnicodeError, ValueError):
        # Deduplication is defensive metadata processing and must never abort a
        # run because an old/imported row contains a malformed port or bracket.
        return f"raw:{raw.split('#', 1)[0]}"


def _grams(title: str) -> set[str]:
    if len(title) < 3:
        return {title} if title else set()
    return {title[index : index + 3] for index in range(len(title) - 2)}


def _material_change(previous: str, current: str, *, allow_text_change: bool) -> bool:
    if set(_NUMBER_RE.findall(previous)) != set(_NUMBER_RE.findall(current)):
        return True
    previous_terms = {term for term in _MATERIAL_TERMS if term in previous}
    current_terms = {term for term in _MATERIAL_TERMS if term in current}
    if previous_terms != current_terms:
        return True
    if not allow_text_change:
        return False
    matcher = SequenceMatcher(None, previous, current, autojunk=False)
    for operation, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if operation == "replace" and min(a_end - a_start, b_end - b_start) >= 2:
            return True
        if operation in {"insert", "delete"}:
            changed = previous[a_start:a_end] or current[b_start:b_end]
            if len(changed) >= 4 and changed not in _BOILERPLATE_FRAGMENTS:
                return True
    return False


@dataclass(frozen=True, slots=True)
class Fingerprint:
    url: str
    title: str


class DuplicateIndex:
    """Seven-day title/URL duplicate index with conservative short-title rules."""

    def __init__(self, items: list[dict[str, str]] | None = None):
        self._items: list[Fingerprint] = []
        self._by_url: dict[str, list[int]] = defaultdict(list)
        self._by_title: dict[str, list[int]] = defaultdict(list)
        self._by_gram: dict[str, set[int]] = defaultdict(set)
        for item in items or []:
            self.add(item["url"], item["title"])

    def add(self, url: str, title: str) -> None:
        normalized_title = normalize_title(title)
        if not normalized_title:
            return
        fingerprint = Fingerprint(canonical_url(url), normalized_title)
        index = len(self._items)
        self._items.append(fingerprint)
        self._by_url[fingerprint.url].append(index)
        self._by_title[fingerprint.title].append(index)
        if len(fingerprint.title) >= MIN_CROSS_URL_TITLE_LENGTH:
            for gram in _grams(fingerprint.title):
                self._by_gram[gram].add(index)

    def is_duplicate(self, url: str, title: str) -> bool:
        normalized_title = normalize_title(title)
        if not normalized_title:
            return False
        normalized_url = canonical_url(url)

        for index in self._by_url.get(normalized_url, []):
            previous = self._items[index].title
            if previous == normalized_title:
                return True
            similarity = SequenceMatcher(None, previous, normalized_title, autojunk=False).ratio()
            if similarity >= SAME_URL_SIMILARITY and not _material_change(
                previous, normalized_title, allow_text_change=True
            ):
                return True

        # A short generic title on a different URL is insufficient evidence of
        # duplication. This intentionally prefers an occasional repeat over a
        # false positive.
        if len(normalized_title) < MIN_CROSS_URL_TITLE_LENGTH:
            return False
        if self._by_title.get(normalized_title):
            return True

        candidate_counts: Counter[int] = Counter()
        for gram in _grams(normalized_title):
            candidate_counts.update(self._by_gram.get(gram, ()))
        for index, _shared in candidate_counts.most_common(MAX_FUZZY_CANDIDATES):
            previous = self._items[index].title
            length_ratio = min(len(previous), len(normalized_title)) / max(
                len(previous), len(normalized_title)
            )
            if length_ratio < 0.75:
                continue
            material_change = _material_change(
                previous, normalized_title, allow_text_change=False
            )
            if (
                length_ratio >= CROSS_URL_CONTAINMENT
                and (previous in normalized_title or normalized_title in previous)
                and not material_change
            ):
                return True
            similarity = SequenceMatcher(None, previous, normalized_title, autojunk=False).ratio()
            if similarity >= CROSS_URL_SIMILARITY and not material_change:
                return True
        return False
