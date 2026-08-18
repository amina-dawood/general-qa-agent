from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import Settings, settings


@dataclass
class AIUsage:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    models: Dict[str, int] | None = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "models": dict(self.models or {}),
        }

    def add(self, other: "AIUsage") -> None:
        self.requests += other.requests
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cached_tokens += other.cached_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cost_usd += other.cost_usd
        if self.models is None:
            self.models = {}
        for model, count in (other.models or {}).items():
            self.models[model] = self.models.get(model, 0) + count


@dataclass
class AIResult:
    value: Any
    usage: AIUsage
    model: str


class UsageCollector:
    def __init__(self):
        self._lock = threading.Lock()
        self._usage = AIUsage(models={})

    def add(self, usage: AIUsage) -> None:
        with self._lock:
            self._usage.add(usage)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return AIUsage(
                requests=self._usage.requests,
                prompt_tokens=self._usage.prompt_tokens,
                completion_tokens=self._usage.completion_tokens,
                total_tokens=self._usage.total_tokens,
                cached_tokens=self._usage.cached_tokens,
                reasoning_tokens=self._usage.reasoning_tokens,
                cost_usd=self._usage.cost_usd,
                models=dict(self._usage.models or {}),
            ).to_dict()


class AIClient:
    """Lazy OpenAI client with structured output and usage accounting."""

    def __init__(self, config: Settings = settings):
        self.config = config
        self._client: Optional[Any] = None
        self._lock = threading.Lock()

    @property
    def client(self):
        if self._client is None:
            if not self.config.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is not configured.")
            from openai import OpenAI
            with self._lock:
                if self._client is None:
                    self._client = OpenAI(
                        api_key=self.config.openai_api_key,
                        base_url=self.config.openai_base_url or None,
                        timeout=self.config.ai_timeout_seconds,
                        max_retries=self.config.ai_max_retries,
                    )
        return self._client

    def _cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
    ) -> float:
        # Pricing changes over time, so the application never invents pricing.
        # Cost is calculated only when MODEL_PRICING_JSON explicitly configures it.
        pricing = self.config.model_pricing.get(model, {})
        if not isinstance(pricing, dict):
            return 0.0
        input_price = float(pricing.get("input", 0.0) or 0.0)
        output_price = float(pricing.get("output", 0.0) or 0.0)
        cached_price = float(pricing.get("cached_input", input_price) or 0.0)
        uncached = max(prompt_tokens - cached_tokens, 0)
        return (
            uncached * input_price
            + cached_tokens * cached_price
            + completion_tokens * output_price
        ) / 1_000_000

    def _usage_from_response(self, response: Any, model: str) -> AIUsage:
        raw = getattr(response, "usage", None)
        prompt = int(getattr(raw, "prompt_tokens", 0) or 0)
        completion = int(getattr(raw, "completion_tokens", 0) or 0)
        total = int(getattr(raw, "total_tokens", prompt + completion) or (prompt + completion))
        cached = 0
        reasoning = 0
        prompt_details = getattr(raw, "prompt_tokens_details", None)
        if prompt_details is not None:
            cached = int(getattr(prompt_details, "cached_tokens", 0) or 0)
        completion_details = getattr(raw, "completion_tokens_details", None)
        if completion_details is not None:
            reasoning = int(getattr(completion_details, "reasoning_tokens", 0) or 0)
        return AIUsage(
            requests=1,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cached_tokens=cached,
            reasoning_tokens=reasoning,
            cost_usd=self._cost(model, prompt, completion, cached),
            models={model: 1},
        )

    def _sampling_kwargs(self, model: str, temperature: float | None) -> Dict[str, Any]:
        lowered = model.lower()
        if temperature is None or lowered.startswith(("gpt-5", "o1", "o3", "o4")):
            return {}
        return {"temperature": temperature}

    def text(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float | None = None,
    ) -> AIResult:
        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **self._sampling_kwargs(model, temperature),
        )
        content = response.choices[0].message.content or ""
        return AIResult(content.strip(), self._usage_from_response(response, model), model)

    def structured(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema_name: str,
        schema: Dict[str, Any],
        temperature: float | None = 0.0,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
    ) -> AIResult:
        request_client = self.client
        if timeout_seconds is not None or max_retries is not None:
            options: Dict[str, Any] = {}
            if timeout_seconds is not None:
                options["timeout"] = max(1.0, float(timeout_seconds))
            if max_retries is not None:
                options["max_retries"] = max(0, int(max_retries))
            request_client = request_client.with_options(**options)

        response = request_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
            **self._sampling_kwargs(model, temperature),
        )
        content = response.choices[0].message.content or "{}"
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Model returned invalid structured JSON: {exc}") from exc
        return AIResult(value, self._usage_from_response(response, model), model)

    def pydantic_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema_type: Any,
    ) -> AIResult:
        """Return a Pydantic instance for DeepEval's runtime schema."""
        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system + " Return only a valid JSON object."},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        try:
            value = schema_type.model_validate_json(content)
        except Exception as exc:
            raise RuntimeError(f"Evaluator model returned invalid structured output: {exc}") from exc
        return AIResult(value, self._usage_from_response(response, model), model)

    def embeddings(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        response = self.client.embeddings.create(
            model=self.config.embedding_model,
            input=texts,
        )
        return [item.embedding for item in response.data]
