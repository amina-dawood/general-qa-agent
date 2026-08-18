from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_NAMES = {
    ".env",
    "qa_agent.db",
    "qa_agent.db-wal",
    "qa_agent.db-shm",
}
FORBIDDEN_SUFFIXES = {".pem", ".p12", ".pfx", ".sqlite", ".sqlite3"}
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".txt", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".bat", ".sh", ".html", ".css", ".example"
}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "__pycache__", ".pytest_cache"}

PATTERNS = {
    "OpenAI-style secret key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private email": re.compile(r"\b[A-Z0-9._%+-]+@(?!example\.(?:com|test)\b)[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "private project identifier": re.compile(r"\b(?:sidelinesavings|heysammi|sammi)\b", re.I),
    "private hosted workflow domain": re.compile(r"\bn8n\.cloud\b", re.I),
}

errors: list[str] = []

for path in ROOT.rglob("*"):
    if path.resolve() == Path(__file__).resolve():
        continue
    if any(part in SKIP_DIRS for part in path.parts):
        continue
    if not path.is_file():
        continue
    rel = path.relative_to(ROOT)
    lower_name = path.name.lower()
    if lower_name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
        errors.append(f"forbidden file: {rel}")
        continue
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".gitignore":
        continue
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    for label, pattern in PATTERNS.items():
        if pattern.search(text):
            errors.append(f"{label}: {rel}")

if errors:
    print("PUBLIC REPO CHECK FAILED")
    for item in sorted(set(errors)):
        print(f" - {item}")
    sys.exit(1)

print("PUBLIC REPO CHECK PASSED")
print("No blocked secret files or obvious private identifiers were found.")
