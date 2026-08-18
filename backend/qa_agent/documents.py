from __future__ import annotations

import hashlib
import heapq
import math
import re
import threading
from array import array
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .ai_client import AIClient
from .config import Settings, settings
from .db import Database, database
from .utils import new_id, utc_now

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == ".docx":
        import docx2txt
        return docx2txt.process(str(path)) or ""
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    raise ValueError(f"Unsupported document type: {suffix}")


def split_text(text: str, chunk_chars: int, overlap_chars: int) -> List[str]:
    normalized = re.sub(r"\r\n?", "\n", text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", normalized) if p.strip()]
    chunks: List[str] = []
    buffer = ""
    for paragraph in paragraphs:
        candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
        if len(candidate) <= chunk_chars:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
        if len(paragraph) <= chunk_chars:
            buffer = paragraph
            continue
        start = 0
        step = max(1, chunk_chars - overlap_chars)
        while start < len(paragraph):
            part = paragraph[start : start + chunk_chars].strip()
            if part:
                chunks.append(part)
            start += step
        buffer = ""
    if buffer:
        chunks.append(buffer)
    return chunks


def normalize_embedding(values: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(float(v) * float(v) for v in values)) or 1.0
    return [float(v) / norm for v in values]


def pack_embedding(values: Iterable[float]) -> bytes:
    return array("f", values).tobytes()


def unpack_embedding(blob: bytes) -> array:
    values = array("f")
    values.frombytes(blob)
    return values


class DocumentService:
    """Low-overhead local document ingestion and semantic retrieval.

    - Documents are indexed sequentially to bound CPU/memory and API bursts.
    - Exact duplicate files are reused.
    - Stale `indexing` state is recovered after process restart.
    - Retrieval uses one batched embedding call for all search intents.
    - Retrieval deliberately keeps source diversity so one long document cannot
      crowd every other requirements document out of the prompt.
    """

    def __init__(
        self,
        db: Database = database,
        ai: AIClient | None = None,
        config: Settings = settings,
    ):
        self.db = db
        self.ai = ai or AIClient(config)
        self.config = config
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.db.execute(
            "UPDATE documents SET status='uploaded' WHERE status='indexing' AND chunk_count=0"
        )

    def _stored_path(self, raw: str | Path) -> Path:
        """Resolve both old Windows paths and new portable relative paths."""
        text = str(raw or "").strip()
        if not text:
            return self.config.upload_dir
        # Older Windows-saved rows may contain backslashes.
        normalized = text.replace("\\", "/")
        path = Path(normalized).expanduser()
        if not path.is_absolute():
            path = self.config.root_dir / path
        return path.resolve()

    def _portable_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.config.root_dir.resolve()).as_posix()
        except ValueError:
            return resolved.as_posix()

    def register_upload(self, project_id: str, filename: str, content: bytes) -> Dict[str, Any]:
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError("Supported documents: PDF, DOCX, TXT, MD.")
        if not content:
            raise ValueError("Uploaded document is empty.")

        checksum = hashlib.sha256(content).hexdigest()
        existing = self.db.find_document_by_checksum(project_id, checksum)
        if existing:
            path = self._stored_path(existing["path"])
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                # Modernize the stored path as we touch this row.
                existing = dict(existing)
                existing["path"] = self._portable_path(path)
                self.db.update_document_path(existing["id"], existing["path"])
            if existing.get("status") != "ready" or int(existing.get("chunk_count", 0) or 0) <= 0:
                self.db.update_document_status(existing["id"], "uploaded", 0)
                existing = dict(existing)
                existing.update(status="uploaded", chunk_count=0)
            return existing

        project_dir = self.config.upload_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        document_id = new_id("doc")
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)
        path = project_dir / f"{document_id}_{safe_name}"
        path.write_bytes(content)
        document = {
            "id": document_id,
            "project_id": project_id,
            "name": Path(filename).name,
            "path": self._portable_path(path),
            "checksum": checksum,
            "status": "uploaded",
            "chunk_count": 0,
            "created_at": utc_now(),
        }
        self.db.save_document(document)
        return document

    def _document_lock(self, document_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(document_id, threading.Lock())

    def _current_document(self, project_id: str, document_id: str) -> Dict[str, Any] | None:
        row = self.db.one(
            "SELECT * FROM documents WHERE id=? AND project_id=?",
            (document_id, project_id),
        )
        return dict(row) if row else None

    def index_document(self, document: Dict[str, Any], progress=None) -> Dict[str, Any]:
        with self._document_lock(document["id"]):
            current = self._current_document(document["project_id"], document["id"])
            if not current:
                raise ValueError("Document no longer exists.")
            if current.get("status") == "ready" and int(current.get("chunk_count", 0) or 0) > 0:
                return current

            path = self._stored_path(current["path"])
            if not path.is_file():
                self.db.update_document_status(current["id"], "error", 0)
                raise ValueError(f"Uploaded file is missing for {current['name']}.")

            self.db.update_document_status(current["id"], "indexing", 0)
            try:
                text = extract_text(path)
                if not text.strip():
                    raise ValueError(f"No readable text found in {current['name']}.")
                chunks = split_text(text, self.config.rag_chunk_chars, self.config.rag_overlap_chars)
                if not chunks:
                    raise ValueError(f"No chunks produced for {current['name']}.")

                rows: List[Dict[str, Any]] = []
                batch_size = self.config.embedding_batch_size
                for start in range(0, len(chunks), batch_size):
                    batch = chunks[start : start + batch_size]
                    embeddings = self.ai.embeddings(batch)
                    if len(embeddings) != len(batch):
                        raise RuntimeError("Embedding provider returned an unexpected vector count.")
                    dimensions = {len(embedding) for embedding in embeddings}
                    if len(dimensions) != 1 or not dimensions or next(iter(dimensions)) <= 0:
                        raise RuntimeError("Embedding provider returned inconsistent vector dimensions.")
                    embedding_dimensions = next(iter(dimensions))
                    for offset, (chunk, embedding) in enumerate(zip(batch, embeddings)):
                        rows.append(
                            {
                                "chunk_index": start + offset,
                                "text": chunk,
                                "embedding": pack_embedding(normalize_embedding(embedding)),
                                "metadata": {
                                    "document_name": current["name"],
                                    "embedding_model": self.config.embedding_model,
                                    "embedding_dimensions": embedding_dimensions,
                                },
                            }
                        )
                    if progress:
                        progress(
                            min(95, int((start + len(batch)) / len(chunks) * 95)),
                            f"Indexing {current['name']}...",
                        )

                self.db.replace_chunks(current["project_id"], current["id"], rows)
                self.db.update_document_status(current["id"], "ready", len(rows))
                result = dict(current)
                result.update(status="ready", chunk_count=len(rows), path=self._portable_path(path))
                # Modernize a legacy path without replacing the document row;
                # replacing the row could cascade-delete its freshly written chunks.
                self.db.update_document_path(current["id"], result["path"])
                return result
            except Exception:
                if self._current_document(current["project_id"], current["id"]):
                    self.db.update_document_status(current["id"], "error", 0)
                raise

    def index_documents(self, items: Sequence[Dict[str, Any]], progress=None) -> Dict[str, Any]:
        documents = list({item["id"]: item for item in items}.values())
        ready: List[Dict[str, Any]] = []
        failed: List[Dict[str, str]] = []
        total = len(documents)
        for index, document in enumerate(documents, start=1):
            base = int((index - 1) / max(1, total) * 95)
            if progress:
                progress(base, f"Indexing {document['name']}...")

            def doc_progress(value: int, message: str) -> None:
                if not progress:
                    return
                span = 95 / max(1, total)
                combined = int(base + (max(0, min(value, 95)) / 100) * span)
                progress(min(95, combined), message)

            try:
                ready.append(self.index_document(document, doc_progress))
            except Exception as exc:
                failed.append({"id": document["id"], "name": document["name"], "error": str(exc)})
        return {
            "total": total,
            "ready_count": len(ready),
            "failed_count": len(failed),
            "ready": ready,
            "failed": failed,
        }

    def pending_documents(self, project_id: str) -> List[Dict[str, Any]]:
        return [
            item
            for item in self.db.list_documents(project_id)
            if item.get("status") != "ready" or int(item.get("chunk_count", 0) or 0) <= 0
        ]

    def remove_document(self, project_id: str, document_id: str) -> bool:
        row = self.db.one(
            "SELECT * FROM documents WHERE id=? AND project_id=?",
            (document_id, project_id),
        )
        if not row:
            return False
        path = self._stored_path(row["path"])
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"Could not remove uploaded file: {exc}") from exc
        self.db.execute(
            "DELETE FROM documents WHERE id=? AND project_id=?",
            (document_id, project_id),
        )
        return True

    def retrieve(self, project_id: str, query: str, top_k: int | None = None) -> Tuple[str, List[Dict[str, Any]]]:
        return self.retrieve_many(project_id, [query], top_k=top_k)

    def retrieve_many(
        self,
        project_id: str,
        queries: Sequence[str],
        top_k: int | None = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        rows = self.db.project_chunks(project_id)
        if not rows:
            raise ValueError("No indexed project documents are available.")

        clean_queries: List[str] = []
        seen = set()
        for query in queries:
            value = " ".join(str(query or "").split())
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                clean_queries.append(value)
        if not clean_queries:
            raise ValueError("A retrieval query is required.")

        query_vectors = [normalize_embedding(item) for item in self.ai.embeddings(clean_queries)]
        if not query_vectors or not query_vectors[0]:
            raise RuntimeError("Embedding provider returned no query vector.")
        query_dimension = len(query_vectors[0])
        if any(len(vector) != query_dimension for vector in query_vectors):
            raise RuntimeError("Embedding provider returned inconsistent query vector dimensions.")
        limit = max(1, int(top_k or self.config.rag_top_k))

        # Maintain small bounded structures instead of sorting all chunk payloads.
        global_heap: List[Tuple[float, int, Any, List[float]]] = []
        best_by_doc: Dict[str, Tuple[float, int, Any, List[float]]] = {}
        best_by_query: List[Tuple[float, int, Any, List[float]] | None] = [None] * len(query_vectors)
        dimension_mismatches = 0

        for row in rows:
            blob = row["embedding"]
            if not blob:
                continue
            embedding = unpack_embedding(blob)
            if len(embedding) != query_dimension:
                dimension_mismatches += 1
                continue
            scores = [
                sum(q * stored for q, stored in zip(query, embedding))
                for query in query_vectors
            ]
            score = max(scores)
            item = (float(score), int(row["id"]), row, scores)

            doc_id = str(row["document_id"])
            previous = best_by_doc.get(doc_id)
            if previous is None or item[:2] > previous[:2]:
                best_by_doc[doc_id] = item

            for index, query_score in enumerate(scores):
                current = best_by_query[index]
                candidate = (float(query_score), int(row["id"]), row, scores)
                if current is None or candidate[:2] > current[:2]:
                    best_by_query[index] = candidate

            if len(global_heap) < limit:
                heapq.heappush(global_heap, item)
            elif item[:2] > global_heap[0][:2]:
                heapq.heapreplace(global_heap, item)

        chosen: Dict[int, Tuple[float, int, Any, List[float]]] = {}

        # 1) Keep one strong chunk from each source document when the budget allows.
        for item in sorted(best_by_doc.values(), key=lambda x: x[:2], reverse=True):
            if len(chosen) >= limit:
                break
            chosen[item[1]] = item

        # 2) Preserve the best evidence for each retrieval intent.
        for item in best_by_query:
            if item is None or len(chosen) >= limit:
                break
            chosen[item[1]] = item

        # 3) Fill the remaining budget by overall semantic relevance.
        for item in sorted(global_heap, key=lambda x: x[:2], reverse=True):
            if len(chosen) >= limit:
                break
            chosen[item[1]] = item

        selected = sorted(chosen.values(), key=lambda x: x[:2], reverse=True)[:limit]
        if not selected and dimension_mismatches:
            raise ValueError(
                "Indexed document embeddings use a different vector dimension from the configured "
                "embedding model. Re-index the project documents after changing EMBEDDING_MODEL."
            )
        docs = {item["id"]: item for item in self.db.list_documents(project_id)}
        refs: List[Dict[str, Any]] = []
        context_parts: List[str] = []
        for rank, (score, _row_id, row, _scores) in enumerate(selected, start=1):
            document = docs.get(row["document_id"], {})
            ref = {
                "document_id": row["document_id"],
                "document": document.get("name", row["document_id"]),
                "chunk_index": int(row["chunk_index"]),
                "score": round(score, 4),
            }
            refs.append(ref)
            context_parts.append(
                f"[SOURCE {rank}: {ref['document']} / chunk {ref['chunk_index']}]\n{row['text']}"
            )
        return "\n\n".join(context_parts), refs
