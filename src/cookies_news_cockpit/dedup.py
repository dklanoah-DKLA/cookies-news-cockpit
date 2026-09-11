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
    "草案",
    "提案",
    "征求意见",
    "通过",
    "生效",
    "执行",
    "实施",
    "延后",
    "延期",
    "推迟",
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
    "draft",
    "proposed",
    "passed",
    "scheduled",
    "effective",
    "implemented",
    "enforced",
    "delayed",
    "postponed",
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
_NUMBER_RE = re.compile(r"(?<!\d)[+-]?\d+(?:,\d{3})*(?:\.\d+)?")
_NUMBER_UNIT_RE = re.compile(
    r"\s*(%|per\s+cent\b|percent\b|basis\s+points?\b|bps\b|"
    r"(?:thousand|million|billion|trillion)\b|(?:usd|eur|cny|rmb|gbp)\b|"
    r"(?:[kmg]w(?:h)?|kg|km|tonnes?|tons?|dollars?|euros?)\b|"
    r"(?:万|亿)?(?:元|美元|欧元|吨|人|台|辆|件)|万|亿|年|月|日|天|小时|分钟|秒)"
)
_CURRENCY_PREFIX_RE = re.compile(r"(?:[$€£¥]|\b(?:usd|eur|cny|rmb|gbp))\s*$")
_MONTH_GROUPS = (
    ("january", "jan"),
    ("february", "feb"),
    ("march", "mar"),
    ("april", "apr"),
    ("may",),
    ("june", "jun"),
    ("july", "jul"),
    ("august", "aug"),
    ("september", "sept", "sep"),
    ("october", "oct"),
    ("november", "nov"),
    ("december", "dec"),
)
_CALENDAR_TERMS = {
    alias: f"month-{number}" for number, aliases in enumerate(_MONTH_GROUPS, 1) for alias in aliases
}
_CALENDAR_TERMS.update(
    {
        term: term
        for term in (
            "yesterday",
            "today",
            "tomorrow",
            "昨日",
            "今日",
            "明日",
            "昨天",
            "今天",
            "明天",
            "上月",
            "本月",
            "下月",
            "去年",
            "今年",
            "明年",
        )
    }
)
_CALENDAR_TERMS.update(
    {
        f"{name}月": f"month-{number}"
        for number, name in enumerate(
            ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"), 1
        )
    }
)
_UNIT_EQUIVALENTS = {
    "percent": "%",
    "per cent": "%",
    "basis point": "bps",
    "basis points": "bps",
    "dollar": "usd",
    "dollars": "usd",
    "$": "usd",
    "美元": "usd",
    "euro": "eur",
    "euros": "eur",
    "€": "eur",
    "欧元": "eur",
    "rmb": "cny",
    "¥": "cny",
    "元": "cny",
    "£": "gbp",
    "ton": "tonne",
    "tons": "tonne",
    "tonnes": "tonne",
    "吨": "tonne",
}


def _normalized_title_text(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold().strip()
    normalized = normalized.replace("−", "-")
    leading_label = _LEADING_LABEL_RE.match(normalized)
    if leading_label:
        label = leading_label.group()
        material_label = _NUMBER_RE.search(label) or any(
            _contains_term(label, term) for term in (*_MATERIAL_TERMS, *_CALENDAR_TERMS)
        )
        if not material_label:
            normalized = normalized[leading_label.end() :]
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
    return normalized


def _canonical_number(number: str) -> str:
    number = number.replace(",", "")
    sign = "-" if number.startswith("-") else ""
    whole, dot, fraction = number.lstrip("+-").partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    value = whole + (dot + fraction if fraction else "")
    return sign + value if value != "0" else "0"


def normalize_title(title: str) -> str:
    normalized = _normalized_title_text(title)
    pieces: list[str] = []
    last = 0
    # Keep numeric punctuation in the identity shared by dedup, report hashes
    # and AI cache keys. Removing it makes 1.5 and 15 the same identity.
    for match in _NUMBER_RE.finditer(normalized):
        between = _compact_text(normalized[last : match.start()])
        if last and not between and normalized[last - 1].isdigit():
            # Keep adjacent numeric tokens separate, e.g. 2026-1-11 must not
            # collapse to the same title/hash as 2026-11-1.
            pieces.append("/")
        pieces.append(between)
        pieces.append(_canonical_number(match.group()))
        last = match.end()
        unit = _NUMBER_UNIT_RE.match(normalized[last:])
        if unit:
            unit_text = " ".join(unit.group(1).split())
            pieces.append(_UNIT_EQUIVALENTS.get(unit_text, unit_text))
            last += unit.end()
    pieces.append(_compact_text(normalized[last:]))
    return "".join(pieces)


def _compact_text(text: str) -> str:
    return "".join(char if char.isalnum() else _UNIT_EQUIVALENTS.get(char, "") for char in text)


@dataclass(frozen=True, slots=True)
class MaterialSignature:
    numbers: tuple[tuple[str, str, str], ...]
    calendar: tuple[str, ...]
    stages: frozenset[str]


def _contains_term(text: str, term: str) -> bool:
    if term.isascii():
        return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) is not None
    return term in text


def _calendar_signature(text: str) -> tuple[str, ...]:
    matches = []
    for term, value in _CALENDAR_TERMS.items():
        pattern = re.escape(term)
        if term.isascii():
            pattern = rf"(?<![a-z]){pattern}(?![a-z])"
        matches.extend((match.start(), match.end(), value) for match in re.finditer(pattern, text))
    result = []
    last_end = 0
    # Preserve time order (June to July is not July to June), and prefer 十二月
    # over its overlapping 二月 substring.
    for start, end, value in sorted(matches, key=lambda match: (match[0], -match[1])):
        if start >= last_end:
            result.append(value)
            last_end = end
    return tuple(result)


def _material_signature(title: str) -> MaterialSignature:
    # Extract before punctuation/whitespace removal, including units and months
    # that a fuzzy text match can otherwise mistake for a syndication rewrite.
    text = unicodedata.normalize("NFKC", title).casefold().replace("−", "-")
    numbers = []
    for match in _NUMBER_RE.finditer(text):
        prefix = _CURRENCY_PREFIX_RE.search(text[: match.start()])
        unit = _NUMBER_UNIT_RE.match(text[match.end() :])
        prefix_text = prefix.group().strip() if prefix else ""
        unit_text = " ".join(unit.group(1).split()) if unit else ""
        numbers.append(
            (
                _canonical_number(match.group()),
                _UNIT_EQUIVALENTS.get(prefix_text, prefix_text),
                _UNIT_EQUIVALENTS.get(unit_text, unit_text),
            )
        )
    return MaterialSignature(
        tuple(numbers),
        _calendar_signature(text),
        frozenset(term for term in _MATERIAL_TERMS if _contains_term(text, term)),
    )


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


def _material_text_change(previous: str, current: str) -> bool:
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
    material: MaterialSignature


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
        fingerprint = Fingerprint(canonical_url(url), normalized_title, _material_signature(title))
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
        material = _material_signature(title)

        for index in self._by_url.get(normalized_url, []):
            fingerprint = self._items[index]
            if fingerprint.material != material:
                continue
            previous = fingerprint.title
            if previous == normalized_title:
                return True
            similarity = SequenceMatcher(None, previous, normalized_title, autojunk=False).ratio()
            if similarity >= SAME_URL_SIMILARITY and not _material_text_change(
                previous, normalized_title
            ):
                return True

        # A short generic title on a different URL is insufficient evidence of
        # duplication. This intentionally prefers an occasional repeat over a
        # false positive.
        if len(normalized_title) < MIN_CROSS_URL_TITLE_LENGTH:
            return False
        if any(
            self._items[index].material == material
            for index in self._by_title.get(normalized_title, [])
        ):
            return True

        candidate_counts: Counter[int] = Counter()
        for gram in _grams(normalized_title):
            candidate_counts.update(self._by_gram.get(gram, ()))
        for index, _shared in candidate_counts.most_common(MAX_FUZZY_CANDIDATES):
            fingerprint = self._items[index]
            if fingerprint.material != material:
                continue
            previous = fingerprint.title
            length_ratio = min(len(previous), len(normalized_title)) / max(
                len(previous), len(normalized_title)
            )
            if length_ratio < 0.75:
                continue
            if length_ratio >= CROSS_URL_CONTAINMENT and (
                previous in normalized_title or normalized_title in previous
            ):
                return True
            similarity = SequenceMatcher(None, previous, normalized_title, autojunk=False).ratio()
            if similarity >= CROSS_URL_SIMILARITY:
                return True
        return False
