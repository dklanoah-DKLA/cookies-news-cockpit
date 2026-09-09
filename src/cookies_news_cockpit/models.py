from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Settings(StrictModel):
    default_threshold: int = Field(default=60, ge=0, le=100)
    default_article_limit: int = Field(default=20, ge=1, le=100)
    freshness_days: Literal[1, 3, 7, 14, 30] | None = 7
    refresh_minutes: int = Field(default=60, ge=15, le=1440)
    scheduler_enabled: bool = True
    extract_full_text: bool = True
    semantic_fallback_enabled: bool = True
    semantic_fallback_limit: int = Field(default=20, ge=1, le=20)
    onboarding_completed: bool = False
    deepseek_status: Literal["unconfigured", "saved_unverified", "connected", "error"] = (
        "unconfigured"
    )
    deepseek_last_tested_at: str | None = None
    deepseek_last_error: str | None = None


class SettingsUpdate(StrictModel):
    default_threshold: int | None = Field(default=None, ge=0, le=100)
    default_article_limit: int | None = Field(default=None, ge=1, le=100)
    freshness_days: Literal[1, 3, 7, 14, 30] | None = None
    refresh_minutes: int | None = Field(default=None, ge=15, le=1440)
    scheduler_enabled: bool | None = None
    extract_full_text: bool | None = None
    semantic_fallback_enabled: bool | None = None
    semantic_fallback_limit: int | None = Field(default=None, ge=1, le=20)
    onboarding_completed: bool | None = None
    deepseek_status: Literal["unconfigured", "saved_unverified", "connected", "error"] | None = None
    deepseek_last_tested_at: str | None = None
    deepseek_last_error: str | None = None


KEYWORD_SEPARATORS = re.compile(r"[,，、;；\r\n]+")


def normalize_keyword_list(values: list[str]) -> list[str]:
    """Normalize UI keyword input while preserving meaningful spaces inside a term."""

    clean: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for part in KEYWORD_SEPARATORS.split(str(raw)):
            value = unicodedata.normalize("NFKC", part)
            value = " ".join(value.strip().split())
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                clean.append(value)
    if len(clean) > 40:
        raise ValueError("Keywords cannot contain more than 40 items")
    return clean


class DeepSeekKeyInput(StrictModel):
    api_key: str = Field(min_length=8, max_length=512)

    @field_validator("api_key")
    @classmethod
    def strip_key(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 8:
            raise ValueError("API key must contain at least 8 non-space characters")
        return value


class DeepSeekTestInput(StrictModel):
    api_key: str | None = Field(default=None, max_length=512)

    @field_validator("api_key")
    @classmethod
    def clean_optional_key(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if len(value) < 8:
            raise ValueError("API key must contain at least 8 non-space characters")
        return value


class TopicInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    keywords: list[str] = Field(default_factory=list, max_length=40)
    exclusion_keywords: list[str] = Field(default_factory=list, max_length=40)
    threshold: int | None = Field(default=None, ge=0, le=100)
    article_limit: int | None = Field(default=None, ge=1, le=100)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Topic name cannot be blank")
        return value

    @field_validator("keywords")
    @classmethod
    def clean_keywords(cls, values: list[str]) -> list[str]:
        return normalize_keyword_list(values)

    @field_validator("exclusion_keywords")
    @classmethod
    def clean_exclusion_keywords(cls, values: list[str]) -> list[str]:
        return normalize_keyword_list(values)


class TopicPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    keywords: list[str] | None = Field(default=None, max_length=40)
    exclusion_keywords: list[str] | None = Field(default=None, max_length=40)
    threshold: int | None = Field(default=None, ge=0, le=100)
    article_limit: int | None = Field(default=None, ge=1, le=100)
    source_ids: list[str] | None = Field(default=None, max_length=100)
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def strip_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Topic name cannot be null")
        value = value.strip()
        if not value:
            raise ValueError("Topic name cannot be blank")
        return value

    @field_validator("keywords")
    @classmethod
    def clean_optional_keywords(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            raise ValueError("Topic keywords cannot be null")
        return TopicInput.clean_keywords(values)

    @field_validator("exclusion_keywords")
    @classmethod
    def clean_optional_exclusion_keywords(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            raise ValueError("Topic exclusion_keywords cannot be null")
        return TopicInput.clean_exclusion_keywords(values)

    @field_validator("source_ids")
    @classmethod
    def reject_null_source_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            raise ValueError("Topic source_ids cannot be null")
        return values

    @field_validator("enabled")
    @classmethod
    def reject_null_enabled(cls, value: bool | None) -> bool | None:
        if value is None:
            raise ValueError("Topic enabled cannot be null")
        return value


class SourceInput(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    url: HttpUrl
    enabled: bool = True
    homepage: HttpUrl | None = None
    category: str = Field(default="custom", min_length=1, max_length=60)
    language: str = Field(default="other", min_length=1, max_length=20)
    terms: HttpUrl | None = None
    preset_id: str | None = Field(default=None, max_length=100)
    preset_version: int | None = Field(default=None, ge=1)
    user_modified: bool = True

    @field_validator("name")
    @classmethod
    def strip_source_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Source name cannot be blank")
        return value


class SourcePatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: HttpUrl | None = None
    enabled: bool | None = None
    homepage: HttpUrl | None = None
    category: str | None = Field(default=None, min_length=1, max_length=60)
    language: str | None = Field(default=None, min_length=1, max_length=20)
    terms: HttpUrl | None = None
    preset_id: str | None = Field(default=None, max_length=100)
    preset_version: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def strip_optional_source_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Source name cannot be blank")
        return value


class CalibrationRequest(StrictModel):
    goal: str = Field(default="", max_length=2000)
    positive_examples: list[str] = Field(default_factory=list, max_length=20)
    negative_examples: list[str] = Field(default_factory=list, max_length=20)


class CalibrationConfirm(StrictModel):
    calibration_id: str
    keywords: list[str] | None = Field(default=None, max_length=40)
    threshold: int | None = Field(default=None, ge=0, le=100)


class TopicSuggestionInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    keywords: list[str] = Field(default_factory=list, max_length=40)
    exclusion_keywords: list[str] = Field(default_factory=list, max_length=40)
    goal: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return TopicInput.strip_name(value)

    @field_validator("keywords", "exclusion_keywords")
    @classmethod
    def clean_keyword_fields(cls, values: list[str]) -> list[str]:
        return normalize_keyword_list(values)


class RunRequest(StrictModel):
    topic_ids: list[str] | None = Field(default=None, max_length=10)
    article_limit: int | None = Field(default=None, ge=1, le=100)


class ImportApplyInput(StrictModel):
    strategy: Literal["merge_keep_local"] = "merge_keep_local"


class FavoriteUpdate(StrictModel):
    favorite: bool


class FeedArticle(StrictModel):
    title: str
    url: str
    excerpt: str = ""
    published_at: str | None = None
    source_id: str
    source_name: str
    full_text: str = ""


class AIAnalysis(StrictModel):
    score: int = Field(ge=0, le=100)
    summary: str = Field(min_length=1, max_length=2000)
    analysis: str = Field(min_length=1, max_length=5000)

    @field_validator("summary", "analysis")
    @classmethod
    def strip_nonblank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("AI analysis text cannot be blank")
        return value


class CalibrationProposal(StrictModel):
    keywords: list[str] = Field(min_length=1, max_length=40)
    threshold: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, max_length=3000)

    @field_validator("keywords")
    @classmethod
    def clean_nonblank_keywords(cls, values: list[str]) -> list[str]:
        cleaned = TopicInput.clean_keywords(values)
        if not cleaned:
            raise ValueError("Calibration keywords cannot be blank")
        return cleaned

    @field_validator("rationale")
    @classmethod
    def strip_nonblank_rationale(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Calibration rationale cannot be blank")
        return value


JsonDict = dict[str, Any]
