from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    details: str = ""


@dataclass(frozen=True)
class SmsJob:
    id: int
    receiver_phone: str
    content: str
    attempt_token: str
    device_serial: str
    send_type: int
    scheduled_at: datetime | None


@dataclass(frozen=True)
class UiTarget:
    x: int
    y: int
    resource_id: str
    text: str
    content_description: str
    score: int

    @property
    def label(self) -> str:
        return self.resource_id or self.content_description or self.text or "unnamed"

