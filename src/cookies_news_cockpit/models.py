from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Settings(StrictModel):
    default_threshold: int = Field(default=60, ge=0, le=100)
    default_article_limit: int = Field(default=20, ge=1, le=100)
    refresh_minutes: int = Field(default=60, ge=15, le=1440)
    scheduler_enabled: bool = True
    extract_full_text: bool = True


class SettingsUpdate(StrictModel):
    default_threshold: int | None = Field(default=None, ge=0, le=100)
    default_article_limit: int | None = Field(default=None, ge=1, le=100)
    refresh_minutes: int | None = Field(default=None, ge=15, le=1440)
    scheduler_enabled: bool | None = None
    extract_full_text: bool | None = None


class DeepSeekKeyInput(StrictModel):
    api_key: str = Field(min_length=8, max_length=512)

    @field_validator("api_key")
    @classmethod
    def strip_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("API key cannot be blank")
        return value


class TopicInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    keywords: list[str] = Field(default_factory=list, max_length=40)
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
        clean: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = raw.strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                clean.append(value)
        return clean


class TopicPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    keywords: list[str] | None = Field(default=None, max_length=40)
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


class RunRequest(StrictModel):
    topic_ids: list[str] | None = Field(default=None, max_length=10)
    article_limit: int | None = Field(default=None, ge=1, le=100)


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
