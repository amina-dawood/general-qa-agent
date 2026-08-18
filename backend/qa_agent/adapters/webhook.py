from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from typing import Any, Dict, Iterable

import httpx

from .base import TargetBlockedError, TargetError, TargetReply

BLOCKED_STATUS_CODES = {408, 409, 425, 429}


def _dot_get(value: Any, path: str) -> Any:
    current = value
    for part in (item for item in path.split(".") if item):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(path)
    return current


def _walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_dicts(nested)


def _extract_reply(response: httpx.Response, response_path: str = "") -> str:
    text = response.text.strip()
    if not text:
        raise TargetError("Target returned an empty response body.")
    try:
        data = response.json()
    except ValueError:
        return text

    if response_path:
        try:
            found = _dot_get(data, response_path)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise TargetError(
                f"Configured response path '{response_path}' did not match the target response."
            ) from exc
        if isinstance(found, str) and found.strip():
            return found.strip()
        raise TargetError("Configured response path did not contain text.")

    for candidate in _walk_dicts(data):
        for array_key in ("last_messages", "messages", "conversation"):
            messages = candidate.get(array_key)
            if not isinstance(messages, list):
                continue
            for item in reversed(messages):
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "").lower()
                if role not in {"assistant", "bot", "agent"}:
                    continue
                content = str(
                    item.get("content") or item.get("text") or item.get("message") or item.get("reply") or ""
                ).strip()
                if content:
                    return content

    for candidate in _walk_dicts(data):
        for key in (
            "reply",
            "reply_text",
            "assistant_message",
            "response",
            "answer",
            "message",
            "content",
            "text",
            "output",
        ):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise TargetError(
        "Target returned JSON but no assistant reply could be identified. Configure response_path."
    )


def _headers(config: Dict[str, Any]) -> Dict[str, str]:
    headers = {str(key): str(value) for key, value in (config.get("headers") or {}).items()}
    for header, env_name in (config.get("header_env") or {}).items():
        value = os.getenv(str(env_name), "")
        if value:
            headers[str(header)] = value
    return headers


def _config_or_env(config: Dict[str, Any], config_key: str, env_key: str, default: str = "") -> str:
    configured = str(config.get(config_key) or "").strip()
    if configured:
        return configured
    return os.getenv(env_key, "").strip() or default


def _raise_for_target_status(response: httpx.Response) -> None:
    if response.status_code in BLOCKED_STATUS_CODES or response.status_code >= 500:
        raise TargetBlockedError(f"Target dependency returned HTTP {response.status_code}.")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise TargetError(
            f"Target returned HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        ) from exc


class GenericWebhookAdapter:
    """Reusable JSON/form webhook adapter with a small persistent connection pool."""

    def __init__(self, target_config: Dict[str, Any]):
        self.config = dict(target_config)
        self.url = str(target_config.get("url") or "").strip()
        if not self.url:
            raise ValueError("Target webhook URL is required.")
        self.timeout = float(target_config.get("timeout_seconds") or 45)
        self.client = httpx.Client(
            headers=_headers(self.config),
            timeout=self.timeout,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=30.0),
        )

    def close(self) -> None:
        self.client.close()

    def start_case(self, test_case: Dict[str, Any], run_id: str, session_id: str) -> None:
        reset_url = str(self.config.get("reset_url") or "").strip()
        if not reset_url:
            return
        try:
            response = self.client.post(
                reset_url,
                json={"run_id": run_id, "test_case_id": test_case["id"], "session_id": session_id},
            )
            _raise_for_target_status(response)
        except (TargetBlockedError, TargetError):
            raise
        except httpx.HTTPError as exc:
            raise TargetBlockedError(f"Reset endpoint unavailable: {exc}") from exc

    def send(
        self,
        message: str,
        test_case: Dict[str, Any],
        run_id: str,
        session_id: str,
    ) -> TargetReply:
        mode = str(self.config.get("payload_mode") or "json").lower()
        message_field = str(self.config.get("message_field") or "message")
        session_field = str(self.config.get("session_field") or "session_id")
        payload = dict(self.config.get("static_payload") or {})
        payload.update(
            {
                message_field: message,
                session_field: session_id,
                "run_id": run_id,
                "test_case_id": test_case["id"],
            }
        )
        started = time.perf_counter()
        try:
            response = self.client.post(self.url, data=payload) if mode == "form" else self.client.post(self.url, json=payload)
            _raise_for_target_status(response)
        except (TargetBlockedError, TargetError):
            raise
        except httpx.HTTPError as exc:
            raise TargetBlockedError(f"Target endpoint unavailable: {exc}") from exc
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return TargetReply(
            _extract_reply(response, str(self.config.get("response_path") or "")),
            {"status_code": response.status_code, "latency_ms": latency_ms, "session_id": session_id},
        )


class TwilioWebhookAdapter(GenericWebhookAdapter):
    """Twilio-style inbound SMS webhook.

    `Body` is always supplied by the AI human simulator. The adapter preserves
    the configured From/To route by default unless a project explicitly opts
    into per-case synthetic identities for fresh-user isolation.
    """

    def __init__(self, target_config: Dict[str, Any]):
        self.base_from_number = str(target_config.get("from_number") or "").strip()
        self.to_number = str(target_config.get("to_number") or "").strip()
        if not self.base_from_number:
            raise ValueError("Twilio-style targets require a From number.")
        if not self.to_number:
            raise ValueError("Twilio-style targets require a To number.")
        self.identity_strategy = str(target_config.get("identity_strategy") or "fixed").strip().lower()
        if self.identity_strategy not in {"fixed", "per_case"}:
            self.identity_strategy = "fixed"
        raw_isolation = target_config.get("isolate_fresh_users")
        if raw_isolation is None:
            # Fresh-user isolation is the safe QA default for simulated inbound
            # Twilio webhooks. It changes only the synthetic From value used by
            # a test case; the saved endpoint, To number and base test sender are
            # never modified.
            self.isolate_fresh_users = True
        elif isinstance(raw_isolation, bool):
            self.isolate_fresh_users = raw_isolation
        else:
            self.isolate_fresh_users = str(raw_isolation).strip().lower() in {
                "1", "true", "yes", "on", "auto", "enabled"
            }
        super().__init__(target_config)

    def _sender_identity(self, test_case: Dict[str, Any], session_id: str) -> str:
        # Returning/continuation cases intentionally retain the configured
        # identity. Fresh-user cases isolate automatically unless the project
        # explicitly disables it. The legacy per_case option remains supported.
        state_mode = str(test_case.get("state_mode") or "fresh_user").strip().lower()
        if state_mode in {"returning_user", "continuation"}:
            return self.base_from_number

        # A real reset endpoint is stronger than synthetic identity isolation:
        # it lets the target clear the known test user explicitly. Preserve the
        # configured sender in that case unless the project deliberately opts
        # into the legacy per_case strategy.
        if str(self.config.get("reset_url") or "").strip() and self.identity_strategy != "per_case":
            return self.base_from_number

        use_synthetic = self.isolate_fresh_users or self.identity_strategy == "per_case"
        if not use_synthetic:
            return self.base_from_number

        digits = re.sub(r"\D", "", self.base_from_number)
        if not digits:
            return self.base_from_number
        has_plus = self.base_from_number.startswith("+")
        # Preserve the configured number's prefix and total length, change only
        # the last 6-8 digits deterministically per test/session.
        suffix_len = min(8, max(6, len(digits) // 2))
        prefix = digits[:-suffix_len]
        seed = f"{test_case.get('id','')}|{session_id}".encode("utf-8")
        suffix = str(int(hashlib.sha256(seed).hexdigest()[:12], 16)).zfill(suffix_len)[-suffix_len:]
        return ("+" if has_plus else "") + prefix + suffix

    def start_case(self, test_case: Dict[str, Any], run_id: str, session_id: str) -> None:
        """Optionally reset target state before a Twilio-style case.

        If no reset URL is configured, this is a no-op. When a reset endpoint
        exists, the payload carries generic QA identifiers for the target system.
        """
        reset_url = str(self.config.get("reset_url") or "").strip()
        if not reset_url:
            return
        sender = self._sender_identity(test_case, session_id)
        payload = {
            "run_id": run_id,
            "test_run_id": run_id,
            "test_case_id": str(test_case.get("id") or ""),
            "session_id": session_id,
            "from_number": sender,
            "channel": _config_or_env(
                self.config, "channel", "QA_TEST_CHANNEL", "Automated_Test_System"
            ),
        }
        headers = _headers(self.config)
        headers.update(
            {
                "X-QA-Run-Id": run_id,
                "X-QA-Test-Case-Id": str(test_case.get("id") or ""),
                "X-QA-Session-Id": session_id,
            }
        )
        try:
            response = self.client.post(reset_url, json=payload, headers=headers)
            _raise_for_target_status(response)
        except (TargetBlockedError, TargetError):
            raise
        except httpx.HTTPError as exc:
            raise TargetBlockedError(f"Reset endpoint unavailable: {exc}") from exc

    def _payload(self, message: str, sender: str) -> Dict[str, str]:
        sid = f"SM{uuid.uuid4().hex}"
        return {
            "ToCountry": _config_or_env(self.config, "to_country", "TWILIO_TO_COUNTRY", "US"),
            "ToState": _config_or_env(self.config, "to_state", "TWILIO_TO_STATE", "CA"),
            "SmsMessageSid": sid,
            "NumMedia": "0",
            "ToCity": _config_or_env(self.config, "to_city", "TWILIO_TO_CITY", ""),
            "FromZip": _config_or_env(self.config, "from_zip", "TWILIO_FROM_ZIP", ""),
            "SmsSid": sid,
            "FromState": _config_or_env(self.config, "from_state", "TWILIO_FROM_STATE", "CA"),
            "SmsStatus": "received",
            "FromCity": _config_or_env(self.config, "from_city", "TWILIO_FROM_CITY", ""),
            "Body": message,
            "FromCountry": _config_or_env(self.config, "from_country", "TWILIO_FROM_COUNTRY", "US"),
            "To": self.to_number,
            "MessagingServiceSid": _config_or_env(
                self.config, "messaging_service_sid", "TWILIO_MESSAGING_SERVICE_SID", "MG_TEST"
            ),
            "ToZip": _config_or_env(self.config, "to_zip", "TWILIO_TO_ZIP", ""),
            "NumSegments": "1",
            "MessageSid": sid,
            "AccountSid": _config_or_env(self.config, "account_sid", "TWILIO_ACCOUNT_SID", "AC_TEST"),
            "From": sender,
            "ApiVersion": _config_or_env(self.config, "api_version", "TWILIO_API_VERSION", "2010-04-01"),
            "Channel": _config_or_env(self.config, "channel", "QA_TEST_CHANNEL", "Automated_Test_System"),
        }

    def send(
        self,
        message: str,
        test_case: Dict[str, Any],
        run_id: str,
        session_id: str,
    ) -> TargetReply:
        sender = self._sender_identity(test_case, session_id)
        payload = self._payload(message, sender)
        headers = _headers(self.config)
        headers.update(
            {
                "X-QA-Run-Id": run_id,
                "X-QA-Test-Case-Id": str(test_case["id"]),
                "X-QA-Session-Id": session_id,
            }
        )
        headers.setdefault("User-Agent", "TwilioProxy/1.1")
        headers.setdefault("I-Twilio-Idempotency-Token", str(uuid.uuid4()))
        started = time.perf_counter()
        try:
            response = self.client.post(self.url, data=payload, headers=headers)
            _raise_for_target_status(response)
        except (TargetBlockedError, TargetError):
            raise
        except httpx.HTTPError as exc:
            raise TargetBlockedError(f"Target endpoint unavailable: {exc}") from exc
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return TargetReply(
            _extract_reply(response, str(self.config.get("response_path") or "")),
            {
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "message_sid": payload["MessageSid"],
                "sender_identity": sender,
                "session_id": session_id,
            },
        )
