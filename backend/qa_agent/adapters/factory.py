from __future__ import annotations

from typing import Any, Dict

from .mock import MockAdapter
from .webhook import GenericWebhookAdapter, TwilioWebhookAdapter


def build_adapter(target_config: Dict[str, Any]):
    adapter = str(target_config.get("adapter") or "generic_webhook").lower()
    if adapter == "mock":
        return MockAdapter()
    if adapter == "twilio_webhook":
        return TwilioWebhookAdapter(target_config)
    if adapter == "generic_webhook":
        return GenericWebhookAdapter(target_config)
    raise ValueError(f"Unsupported target adapter: {adapter}")
