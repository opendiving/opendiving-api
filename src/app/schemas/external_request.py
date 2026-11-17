"""What `GET /admin/external-requests` answers with.

Services, kinds and outcomes are plain strings on the wire rather than enums: the vocabulary
grows with the code, and a client shows a slug it has never heard of as the slug.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class ExternalRequestLimit(BaseModel):
    # The instance's own cap: at most `requests` in any `window_seconds`.
    requests: int
    window_seconds: int
    # The most that cap admits in a UTC day, rounded down.
    per_day: int


class ExternalRequestProvider(BaseModel):
    provider: str
    # Every kind of request the instance can send this service.
    kinds: list[str]
    # `None` for a service the instance sets itself no cap on.
    limit: ExternalRequestLimit | None


class ExternalRequestCount(BaseModel):
    provider: str
    kind: str
    outcome: str
    count: int
    # Whole milliseconds over every request counted here; both `0` for an outcome that sent
    # nothing.
    duration_ms_total: int
    duration_ms_max: int


class ExternalRequestsDay(BaseModel):
    day: date
    # Ordered by provider, kind and outcome; empty for a day with no events.
    requests: list[ExternalRequestCount]


class ExternalRequestsRead(BaseModel):
    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    # `from` is a Python keyword, hence the alias; the wire name is `from`.
    from_: date = Field(alias="from")
    to: date
    providers: list[ExternalRequestProvider]
    days: list[ExternalRequestsDay]
