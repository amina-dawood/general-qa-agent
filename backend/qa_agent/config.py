from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    return _env(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _json(name: str, default: str = "{}") -> Dict[str, Any]:
    try:
        value = json.loads(_env(name, default) or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _path(name: str, default: Path) -> Path:
    raw = _env(name, str(default))
    value = Path(raw).expanduser()
    if not value.is_absolute():
        value = ROOT_DIR / value
    return value.resolve()


@dataclass(frozen=True)
class Settings:
    """Runtime settings with conservative single-node defaults.

    Performance thresholds are operational QA defaults, not product claims.
    A documented SLA can still be captured per generated test case, but it is
    advisory unless the project explicitly opts into enforcing documented SLAs.
    This prevents an unrealistic legacy requirement (for example 1 second) from
    making every otherwise useful conversational test fail automatically.
    """

    root_dir: Path = ROOT_DIR
    data_dir: Path = field(default_factory=lambda: _path("DATA_DIR", ROOT_DIR / "data"))
    database_path: Path = field(default_factory=lambda: _path("DATABASE_PATH", ROOT_DIR / "data" / "qa_agent.db"))
    upload_dir: Path = field(default_factory=lambda: _path("UPLOAD_DIR", ROOT_DIR / "data" / "uploads"))
    report_dir: Path = field(default_factory=lambda: _path("REPORT_DIR", ROOT_DIR / "data" / "reports"))
    dashboard_dist: Path = field(default_factory=lambda: _path("DASHBOARD_DIST", ROOT_DIR / "dashboard" / "dist"))

    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    api_workers: int = field(default_factory=lambda: max(1, min(_int("API_WORKERS", 1), 2)))

    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    generation_model: str = field(default_factory=lambda: _env("GENERATION_MODEL", "gpt-4.1-mini"))
    simulation_model: str = field(default_factory=lambda: _env("SIMULATION_MODEL", "gpt-4.1-mini"))
    evaluation_model: str = field(default_factory=lambda: _env("EVALUATION_MODEL", "gpt-4.1-mini"))
    diagnosis_model: str = field(default_factory=lambda: _env("DIAGNOSIS_MODEL", "gpt-4.1-mini"))
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", "text-embedding-3-small"))
    ai_timeout_seconds: float = field(default_factory=lambda: max(10.0, _float("AI_TIMEOUT_SECONDS", 120.0)))
    ai_max_retries: int = field(default_factory=lambda: max(0, min(_int("AI_MAX_RETRIES", 2), 5)))
    generation_audit_timeout_seconds: float = field(
        default_factory=lambda: max(15.0, min(_float("GENERATION_AUDIT_TIMEOUT_SECONDS", 60.0), 120.0))
    )
    model_pricing: Dict[str, Any] = field(default_factory=lambda: _json("MODEL_PRICING_JSON"))

    max_generated_cases: int = field(default_factory=lambda: max(4, min(_int("MAX_GENERATED_TEST_CASES", 24), 60)))
    max_conversation_turns: int = field(default_factory=lambda: max(2, min(_int("MAX_CONVERSATION_TURNS", 18), 40)))
    max_prompt_chars: int = field(default_factory=lambda: max(8000, min(_int("MAX_PROMPT_CHARS", 28000), 80000)))
    rag_chunk_chars: int = field(default_factory=lambda: max(500, min(_int("RAG_CHUNK_CHARS", 1400), 5000)))
    rag_overlap_chars: int = field(default_factory=lambda: max(0, min(_int("RAG_OVERLAP_CHARS", 180), 1000)))
    rag_top_k: int = field(default_factory=lambda: max(4, min(_int("RAG_TOP_K", 18), 50)))
    embedding_batch_size: int = field(default_factory=lambda: max(1, min(_int("EMBEDDING_BATCH_SIZE", 48), 128)))

    enable_deepeval: bool = field(default_factory=lambda: _bool("ENABLE_DEEPEVAL", True))
    evaluation_threshold: float = field(default_factory=lambda: min(1.0, max(0.0, _float("EVALUATION_THRESHOLD", 0.75))))
    diagnosis_enabled: bool = field(default_factory=lambda: _bool("ENABLE_FAILURE_DIAGNOSIS", True))

    # Generic conversational performance policy. Projects can override these in
    # target configuration without changing Python code.
    performance_warning_ms: int = field(default_factory=lambda: max(250, _int("PERFORMANCE_WARNING_MS", 4000)))
    performance_critical_ms: int = field(default_factory=lambda: max(500, _int("PERFORMANCE_CRITICAL_MS", 8000)))
    performance_fail_ms: int = field(default_factory=lambda: max(1000, _int("PERFORMANCE_FAIL_MS", 20000)))
    enforce_documented_response_sla: bool = field(
        default_factory=lambda: _bool("ENFORCE_DOCUMENTED_RESPONSE_SLA", False)
    )

    job_workers: int = field(default_factory=lambda: max(1, min(_int("JOB_WORKERS", 2), 4)))
    dashboard_cache_seconds: float = field(default_factory=lambda: max(0.2, min(_float("DASHBOARD_CACHE_SECONDS", 2.0), 30.0)))
    max_upload_mb: int = field(default_factory=lambda: max(1, min(_int("MAX_UPLOAD_MB", 25), 100)))

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_directories()

