"""Pydantic request/response schemas for the notifications module."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator


# ── Requests ───────────────────────────────────────────────────────────────────


class SendReportRequest(BaseModel):
    recipients: list[str]
    report_type: str  # leadership | ops | dq | client_health | manual
    filters: dict[str, Any] | None = None
    subject: str | None = None
    client_id: int | None = None


class SendTestEmailRequest(BaseModel):
    recipient: str


class SubscriptionCreate(BaseModel):
    name: str
    report_type: str
    recipients: list[str]
    frequency: str  # daily | weekly | monthly

    @field_validator("frequency")
    @classmethod
    def validate_frequency(cls, v: str) -> str:
        allowed = {"daily", "weekly", "biweekly", "monthly"}
        if v not in allowed:
            raise ValueError(f"frequency must be one of {allowed}")
        return v

    timezone: str = "UTC"
    filters: dict[str, Any] | None = None
    client_id: int | None = None


class SubscriptionUpdate(BaseModel):
    name: str | None = None
    recipients: list[str] | None = None
    frequency: str | None = None
    timezone: str | None = None
    filters: dict[str, Any] | None = None
    is_enabled: bool | None = None


class AlertRuleCreate(BaseModel):
    name: str
    rule_type: str  # publish_conversion_drop | gap_too_high | backlog_high | dq_low | missing_metadata_spike

    @field_validator("rule_type")
    @classmethod
    def validate_rule_type(cls, v: str) -> str:
        allowed = {
            "publish_conversion_drop",
            "gap_too_high",
            "backlog_high",
            "dq_low",
            "missing_metadata_spike",
        }
        if v not in allowed:
            raise ValueError(f"rule_type must be one of {allowed}")
        return v

    threshold_value: float
    comparison_operator: str = "lt"

    @field_validator("comparison_operator")
    @classmethod
    def validate_operator(cls, v: str) -> str:
        allowed = {"lt", "gt", "lte", "gte"}
        if v not in allowed:
            raise ValueError(f"comparison_operator must be one of {allowed}")
        return v

    recipients: list[str]
    cooldown_minutes: int = 360
    filters: dict[str, Any] | None = None


class AlertRuleUpdate(BaseModel):
    name: str | None = None
    threshold_value: float | None = None
    comparison_operator: str | None = None
    recipients: list[str] | None = None
    cooldown_minutes: int | None = None
    filters: dict[str, Any] | None = None
    is_enabled: bool | None = None


# ── Responses ──────────────────────────────────────────────────────────────────


class EmailSendResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    message_id: str | None = None
    status: str


class SubscriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    report_type: str
    recipients: list[str]
    frequency: str
    timezone: str
    filters: dict[str, Any] | None = None
    client_id: int | None = None
    is_enabled: bool
    created_by: str
    last_run_at: int | None = None
    next_run_at: int | None = None
    created_at: int
    updated_at: int


class AlertRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    rule_type: str
    threshold_value: float
    comparison_operator: str
    recipients: list[str]
    cooldown_minutes: int
    filters: dict[str, Any] | None = None
    is_enabled: bool
    created_by: str
    last_triggered_at: int | None = None
    created_at: int
    updated_at: int


class DeliveryLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    subscription_id: uuid.UUID | None = None
    alert_rule_id: uuid.UUID | None = None
    report_type: str
    recipients: list[str]
    status: str
    provider: str
    provider_message_id: str | None = None
    error_text: str | None = None
    created_at: int
