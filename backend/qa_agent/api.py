from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .analytics import project_analytics
from .config import settings
from .db import database
from .documents import DocumentService
from .engine import QAEngine
from .jobs import job_manager
from .reporting import ReportService
from .utils import new_id, slugify, utc_now
from .workflow import WorkflowService


router = APIRouter(
    prefix="/api"
)

documents = DocumentService(
    database
)

engine = QAEngine(
    database,
    documents=documents,
)

workflow_service = (
    WorkflowService()
)

reports = ReportService()

_cache_lock = threading.Lock()

_cache: Dict[
    str,
    tuple[float, Any],
] = {}


def _cache_get(
    key: str,
):
    now = time.monotonic()

    with _cache_lock:
        item = _cache.get(
            key
        )

        if (
            item
            and item[0] > now
        ):
            return item[1]

    return None


def _cache_put(
    key: str,
    value: Any,
):
    with _cache_lock:
        _cache[key] = (
            time.monotonic()
            + settings.dashboard_cache_seconds,
            value,
        )

    return value


def _invalidate(
    project_id: Optional[str] = None,
):
    with _cache_lock:
        if project_id is None:
            _cache.clear()

        else:
            _cache.pop(
                "projects",
                None,
            )

            for key in list(
                _cache
            ):
                if project_id in key:
                    _cache.pop(
                        key,
                        None,
                    )


class ProjectCreate(
    BaseModel
):
    name: str = Field(
        min_length=1,
        max_length=120,
    )

    description: str = ""

    target: Dict[
        str,
        Any,
    ] = Field(
        default_factory=lambda: {
            "adapter":
                "mock"
        }
    )

    fixtures: Dict[
        str,
        Any,
    ] = Field(
        default_factory=dict
    )


class ProjectUpdate(
    BaseModel
):
    name: Optional[str] = None
    description: Optional[str] = None

    target: Optional[
        Dict[str, Any]
    ] = None

    fixtures: Optional[
        Dict[str, Any]
    ] = None


class ProjectResourcesUpdate(BaseModel):
    resources: Dict[str, Any] = Field(default_factory=dict)


class GenerateRequest(
    BaseModel
):
    feature: str = Field(
        default="Full product",
        max_length=120,
    )

    # Legacy focus/query field retained for backward compatibility with older dashboards and API clients.
    query: str = Field(
        default="",
        max_length=1200,
    )

    # Free-form tester instruction used to shape the generated test inventory.
    generation_prompt: str = Field(
        default="",
        max_length=6000,
    )


class ExecuteRequest(
    BaseModel
):
    suite_id: str
    priority: str = "All"

    limit: int = Field(
        default=20,
        ge=1,
        le=100,
    )

    test_case_ids: Optional[
        List[str]
    ] = None


class ReviewRequest(
    BaseModel
):
    status: str
    note: str = ""


class SuiteApproveRequest(
    BaseModel
):
    approve_all_cases: bool = True


class ImproveRequest(
    BaseModel
):
    note: str = Field(
        min_length=3,
        max_length=2000,
    )


@router.get(
    "/health"
)
def health():
    return {
        "ok": True,
        "version": "4.0.0",
        "storage": "sqlite",
        "deepeval":
            settings.enable_deepeval,
    }


@router.get(
    "/projects"
)
def list_projects():
    cached = _cache_get(
        "projects"
    )

    if cached is not None:
        return cached

    items = (
        database.list_projects()
    )

    counts = (
        database.document_counts()
    )

    for item in items:
        item[
            "document_count"
        ] = counts.get(
            item["id"],
            0,
        )

    return _cache_put(
        "projects",
        items,
    )


@router.post(
    "/projects"
)
def create_project(
    payload: ProjectCreate,
):
    project = {
        "id":
            new_id(
                "project"
            ),

        "name":
            payload.name.strip(),

        "slug":
            slugify(
                payload.name
            ),

        "description":
            payload.description.strip(),

        "status":
            "active",

        "target":
            payload.target,

        "fixtures":
            payload.fixtures,

        "workflow":
            None,

        "created_at":
            utc_now(),

        "updated_at":
            utc_now(),
    }

    database.save_project(
        project
    )

    _invalidate()

    return project


@router.get(
    "/projects/{project_id}"
)
def get_project(
    project_id: str,
):
    project = (
        database.get_project(
            project_id
        )
    )

    if not project:
        raise HTTPException(
            404,
            "Project not found",
        )

    project[
        "documents"
    ] = database.list_documents(
        project_id
    )

    return project


@router.patch(
    "/projects/{project_id}"
)
def update_project(
    project_id: str,
    payload: ProjectUpdate,
):
    project = (
        database.get_project(
            project_id
        )
    )

    if not project:
        raise HTTPException(
            404,
            "Project not found",
        )

    for key, value in (
        payload.model_dump(
            exclude_unset=True
        ).items()
    ):
        if value is not None:
            project[key] = value

    if payload.name:
        project[
            "slug"
        ] = slugify(
            payload.name
        )

    database.save_project(
        project
    )

    _invalidate(
        project_id
    )

    _invalidate()

    # Return the full project shape so saving target settings never makes
    # the dashboard temporarily lose the already-uploaded documents.
    project["documents"] = database.list_documents(project_id)
    return project


@router.put(
    "/projects/{project_id}/resources"
)
def save_project_resources(
    project_id: str,
    payload: ProjectResourcesUpdate,
):
    """Persist tester-controlled runtime resources without touching target config.

    Keeping this as a dedicated endpoint makes resource saves explicit and
    prevents an unrelated connection edit from ever overwriting the values.
    The response is the canonical saved resource map so the dashboard can
    verify the round trip before showing a Saved state.
    """

    project = database.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    normalized: Dict[str, Any] = {}
    for raw_key, raw_value in (payload.resources or {}).items():
        key = str(raw_key or "").strip()
        if not key:
            raise HTTPException(400, "Every test resource needs a key.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            raise HTTPException(
                400,
                f'Invalid resource key "{key}". Use letters, numbers, underscore, dot or hyphen only.',
            )
        value = raw_value if not isinstance(raw_value, str) else raw_value.strip()
        if value is None or value == "":
            raise HTTPException(400, f'Test resource "{key}" needs a value.')
        normalized[key] = value

    project["fixtures"] = normalized
    database.save_project(project)
    _invalidate(project_id)
    _invalidate()

    return {
        "ok": True,
        "resources": normalized,
        "saved_at": utc_now(),
    }


@router.get(
    "/projects/{project_id}/documents"
)
def list_documents(
    project_id: str,
):
    return (
        database.list_documents(
            project_id
        )
    )


@router.post(
    "/projects/{project_id}/documents"
)
async def upload_documents(
    project_id: str,
    files: List[
        UploadFile
    ] = File(...),
):
    if not database.get_project(
        project_id
    ):
        raise HTTPException(
            404,
            "Project not found",
        )

    registered_by_id: Dict[
        str,
        Dict[str, Any],
    ] = {}

    max_bytes = (
        settings.max_upload_mb
        * 1024
        * 1024
    )

    for item in files:
        content = await item.read(
            max_bytes + 1
        )

        if len(content) > max_bytes:
            raise HTTPException(
                413,
                (
                    f"{item.filename or 'Document'} "
                    f"exceeds the "
                    f"{settings.max_upload_mb} MB "
                    "upload limit."
                ),
            )

        document = (
            documents.register_upload(
                project_id,
                item.filename
                or "document",
                content,
            )
        )

        registered_by_id[
            document["id"]
        ] = document

    registered = list(
        registered_by_id.values()
    )

    def work(
        progress,
    ):
        result = (
            documents.index_documents(
                registered,
                progress,
            )
        )

        _invalidate(
            project_id
        )

        return result

    return job_manager.submit(
        project_id,
        "index_documents",
        work,
        serial_key=(
            f"documents:{project_id}"
        ),
    )


@router.post(
    "/projects/{project_id}/documents/reindex"
)
def reindex_documents(
    project_id: str,
):
    if not database.get_project(
        project_id
    ):
        raise HTTPException(
            404,
            "Project not found",
        )

    pending = (
        documents.pending_documents(
            project_id
        )
    )

    if not pending:
        return job_manager.submit(
            project_id,
            "reindex_documents",
            lambda progress: {
                "total": 0,
                "ready_count": 0,
                "failed_count": 0,
                "ready": [],
                "failed": [],
            },
            serial_key=(
                f"documents:{project_id}"
            ),
        )

    def work(
        progress,
    ):
        result = (
            documents.index_documents(
                pending,
                progress,
            )
        )

        _invalidate(
            project_id
        )

        return result

    return job_manager.submit(
        project_id,
        "reindex_documents",
        work,
        serial_key=(
            f"documents:{project_id}"
        ),
    )


@router.delete(
    "/projects/{project_id}/documents/{document_id}"
)
def remove_document(
    project_id: str,
    document_id: str,
):
    if not database.get_project(
        project_id
    ):
        raise HTTPException(
            404,
            "Project not found",
        )

    try:
        removed = (
            documents.remove_document(
                project_id,
                document_id,
            )
        )

    except RuntimeError as exc:
        raise HTTPException(
            409,
            str(exc),
        ) from exc

    if not removed:
        raise HTTPException(
            404,
            "Document not found",
        )

    _invalidate(
        project_id
    )

    return {
        "ok": True,
        "document_id":
            document_id,
    }


@router.post(
    "/projects/{project_id}/workflow"
)
async def upload_workflow(
    project_id: str,
    file: UploadFile = File(...),
):
    project = (
        database.get_project(
            project_id
        )
    )

    if not project:
        raise HTTPException(
            404,
            "Project not found",
        )

    max_bytes = (
        min(
            settings.max_upload_mb,
            10,
        )
        * 1024
        * 1024
    )

    raw = await file.read(
        max_bytes + 1
    )

    if len(raw) > max_bytes:
        raise HTTPException(
            413,
            (
                "Workflow JSON exceeds "
                "the 10 MB upload limit."
            ),
        )

    try:
        value = json.loads(
            raw.decode(
                "utf-8"
            )
        )

    except Exception as exc:
        raise HTTPException(
            400,
            (
                "Workflow must be "
                f"valid JSON: {exc}"
            ),
        ) from exc

    if not isinstance(
        value,
        dict,
    ):
        raise HTTPException(
            400,
            "Workflow JSON must be an object",
        )

    project[
        "workflow"
    ] = {
        "filename":
            file.filename
            or "workflow.json",

        "summary":
            workflow_service.summarize(
                value
            ),

        "attached_at":
            utc_now(),
    }

    database.save_project(
        project
    )

    _invalidate(
        project_id
    )

    return project[
        "workflow"
    ]


@router.delete(
    "/projects/{project_id}/workflow"
)
def remove_workflow(
    project_id: str,
):
    project = (
        database.get_project(
            project_id
        )
    )

    if not project:
        raise HTTPException(
            404,
            "Project not found",
        )

    project[
        "workflow"
    ] = None

    database.save_project(
        project
    )

    _invalidate(
        project_id
    )

    return {
        "ok": True
    }


@router.get(
    "/projects/{project_id}/analytics"
)
def analytics(
    project_id: str,
):
    key = (
        f"analytics:{project_id}"
    )

    cached = _cache_get(
        key
    )

    if cached is not None:
        return cached

    if not database.get_project(
        project_id
    ):
        raise HTTPException(
            404,
            "Project not found",
        )

    return _cache_put(
        key,
        project_analytics(
            project_id
        ),
    )


@router.get(
    "/projects/{project_id}/suites"
)
def suites(
    project_id: str,
    limit: int = 100,
):
    return (
        database.list_suites(
            project_id,
            max(
                1,
                min(
                    limit,
                    200,
                ),
            ),
        )
    )


@router.post(
    "/projects/{project_id}/jobs/generate"
)
def generate_suite(
    project_id: str,
    payload: GenerateRequest,
):
    if not database.get_project(
        project_id
    ):
        raise HTTPException(
            404,
            "Project not found",
        )

    def work(
        progress,
    ):
        manual_prompt = payload.generation_prompt.strip() or payload.query.strip()
        retrieval_query = manual_prompt or (
            "Cover the full documented product scope with production happy-path, negative, "
            "validation, recovery, context, integration, boundary and data-integrity risks where supported."
        )
        result = (
            engine.generate_suite(
                project_id,
                payload.feature.strip() or "Full product",
                retrieval_query,
                progress,
                generation_prompt=manual_prompt,
            )
        )

        _invalidate(
            project_id
        )

        return result

    return job_manager.submit(
        project_id,
        "generate_suite",
        work,
    )


@router.post(
    "/suites/{suite_id}/approve"
)
def approve_suite(
    suite_id: str,
    payload:
        SuiteApproveRequest,
):
    suite = (
        database.get_suite(
            suite_id
        )
    )

    if not suite:
        raise HTTPException(
            404,
            "Suite not found",
        )

    if (
        suite.get(
            "status"
        )
        == "rejected"
    ):
        raise HTTPException(
            400,
            (
                "Restore the rejected "
                "suite before approval."
            ),
        )

    if (
        payload.approve_all_cases
    ):
        for case in suite.get(
            "test_cases",
            [],
        ):
            # "Approve suite" is convenient for normal draft cases, but it
            # must not silently overrule an explicit needs-revision decision.
            status = case.get("review_status", "draft")
            if status == "draft":
                case["review_status"] = "approved"
                case["approved"] = True

    approved_cases = [
        case
        for case
        in suite.get(
            "test_cases",
            [],
        )
        if (
            case.get(
                "approved"
            )
            and case.get(
                "review_status"
            )
            == "approved"
        )
    ]

    if not approved_cases:
        raise HTTPException(
            400,
            (
                "At least one test case "
                "must be approved."
            ),
        )

    covered = {
        requirement_id
        for case
        in approved_cases
        for requirement_id
        in case.get(
            "requirement_ids",
            [],
        )
    }

    high_gaps = [
        requirement[
            "id"
        ]
        for requirement
        in suite.get(
            "requirements",
            [],
        )
        if (
            requirement.get(
                "risk"
            )
            == "High"
            and requirement[
                "id"
            ]
            not in covered
        )
    ]

    generation_summary = suite.get("generation_summary") or {}
    coverage_gate_mode = generation_summary.get("coverage_gate_mode", "full-scope")

    # Manual dashboard prompts intentionally create targeted suites. Their
    # generation gate already validates the prompt-relevant scenario contract,
    # so suite approval must not re-apply the old full-scope High-risk gate and
    # block approval because unrelated requirements were not requested. Broad
    # generation keeps the original strict High-risk coverage requirement.
    if high_gaps and coverage_gate_mode != "prompt-targeted":
        raise HTTPException(
            400,
            (
                "High-risk requirements "
                "are not covered by approved tests: "
                + ", ".join(
                    high_gaps
                )
            ),
        )

    suite[
        "approved"
    ] = True

    suite[
        "status"
    ] = "approved"

    database.save_suite(
        suite
    )

    _invalidate(
        suite[
            "project_id"
        ]
    )

    return suite


@router.post(
    "/suites/{suite_id}/reject"
)
def reject_suite(
    suite_id: str,
    payload: ReviewRequest,
):
    suite = (
        database.get_suite(
            suite_id
        )
    )

    if not suite:
        raise HTTPException(
            404,
            "Suite not found",
        )

    if not payload.note.strip():
        raise HTTPException(
            400,
            (
                "A rejection reason "
                "is required."
            ),
        )

    suite[
        "approved"
    ] = False

    suite[
        "status"
    ] = "rejected"

    suite[
        "review_note"
    ] = payload.note.strip()

    database.save_suite(
        suite
    )

    _invalidate(
        suite[
            "project_id"
        ]
    )

    return suite


@router.post(
    "/suites/{suite_id}/restore"
)
def restore_suite(
    suite_id: str,
):
    suite = (
        database.get_suite(
            suite_id
        )
    )

    if not suite:
        raise HTTPException(
            404,
            "Suite not found",
        )

    suite[
        "approved"
    ] = False

    suite[
        "status"
    ] = "draft"

    database.save_suite(
        suite
    )

    _invalidate(
        suite[
            "project_id"
        ]
    )

    return suite


@router.patch(
    "/suites/{suite_id}/cases/{case_id}"
)
def review_case(
    suite_id: str,
    case_id: str,
    payload: ReviewRequest,
):
    suite = (
        database.get_suite(
            suite_id
        )
    )

    if not suite:
        raise HTTPException(
            404,
            "Suite not found",
        )

    allowed = {
        "draft",
        "approved",
        "rejected",
        "needs_revision",
        "deprecated",
    }

    if (
        payload.status
        not in allowed
    ):
        raise HTTPException(
            400,
            "Invalid review status",
        )

    found = False

    for case in suite.get(
        "test_cases",
        [],
    ):
        if (
            case.get(
                "id"
            )
            == case_id
        ):
            found = True

            case[
                "review_status"
            ] = payload.status

            case[
                "approved"
            ] = (
                payload.status
                == "approved"
            )

            case[
                "review_note"
            ] = (
                payload.note.strip()
            )

            break

    if not found:
        raise HTTPException(
            404,
            "Test case not found",
        )

    suite[
        "approved"
    ] = False

    if (
        suite.get(
            "status"
        )
        == "approved"
    ):
        suite[
            "status"
        ] = "draft"

    database.save_suite(
        suite
    )

    _invalidate(
        suite[
            "project_id"
        ]
    )

    return suite


@router.post(
    "/suites/{suite_id}/cases/{case_id}/jobs/improve"
)
def improve_case(
    suite_id: str,
    case_id: str,
    payload: ImproveRequest,
):
    suite = (
        database.get_suite(
            suite_id
        )
    )

    if not suite:
        raise HTTPException(
            404,
            "Suite not found",
        )

    def work(
        progress,
    ):
        result = (
            engine.improve_test_case(
                suite_id,
                case_id,
                payload.note,
                progress,
            )
        )

        _invalidate(
            suite[
                "project_id"
            ]
        )

        return result

    return job_manager.submit(
        suite[
            "project_id"
        ],
        "improve_test_case",
        work,
    )


@router.post(
    "/suites/{suite_id}/jobs/workflow-review"
)
def workflow_review(
    suite_id: str,
):
    """
    Optional workflow-vs-requirements analysis.

    Advisory only.
    It never changes functional test outcomes.
    """

    suite = (
        database.get_suite(
            suite_id
        )
    )

    if not suite:
        raise HTTPException(
            404,
            "Suite not found",
        )

    project = (
        database.get_project(
            suite[
                "project_id"
            ]
        )
    )

    if not project:
        raise HTTPException(
            404,
            "Project not found",
        )

    if not (
        (
            project.get(
                "workflow"
            )
            or {}
        ).get(
            "summary"
        )
    ):
        raise HTTPException(
            400,
            (
                "Attach a workflow JSON "
                "to the project before running "
                "workflow advisory analysis."
            ),
        )

    def work(
        progress,
    ):
        progress(
            20,
            (
                "Comparing workflow structure "
                "with requirements..."
            ),
        )

        review = (
            workflow_service
            .advisory_review(
                project,
                suite.get(
                    "requirements",
                    [],
                ),
            )
        )

        suite[
            "workflow_review"
        ] = {
            **review,
            "reviewed_at":
                utc_now(),
        }

        database.save_suite(
            suite
        )

        usage = (
            review.get(
                "ai_usage"
            )
            or {}
        )

        if usage:
            database.save_usage(
                (
                    "workflow-review:"
                    f"{suite_id}"
                ),
                suite[
                    "project_id"
                ],
                "workflow_review",
                usage,
            )

        _invalidate(
            suite[
                "project_id"
            ]
        )

        progress(
            90,
            (
                "Saving advisory "
                "findings..."
            ),
        )

        return suite

    return job_manager.submit(
        suite[
            "project_id"
        ],
        "workflow_review",
        work,
    )


@router.get(
    "/projects/{project_id}/runs"
)
def list_runs(
    project_id: str,
    limit: int = 50,
):
    if not database.get_project(
        project_id
    ):
        raise HTTPException(
            404,
            "Project not found",
        )

    return (
        database.list_run_summaries(
            project_id,
            max(
                1,
                min(
                    limit,
                    200,
                ),
            ),
        )
    )


@router.get(
    "/runs/{run_id}"
)
def get_run(
    run_id: str,
):
    run = (
        database.get_run(
            run_id
        )
    )

    if not run:
        raise HTTPException(
            404,
            "Run not found",
        )

    return run


@router.delete(
    "/runs/{run_id}"
)
def delete_run(
    run_id: str,
):
    run = database.get_run(run_id)

    if not run:
        raise HTTPException(404, "Run not found")

    status = str(run.get("status") or "").lower()
    if status in {"running", "awaiting_human"}:
        raise HTTPException(
            409,
            "Active or human-paused runs cannot be deleted. Finish or cancel the run first.",
        )

    project_id = str(run.get("project_id") or "")

    # Delete the database record first. Report cleanup is best-effort so a
    # filesystem permission issue cannot leave a supposedly deleted run visible.
    removed = database.delete_run(run_id)
    if not removed:
        raise HTTPException(404, "Run not found")

    report_root = settings.report_dir.resolve()
    candidates = {
        report_root / f"{run_id}.json",
        report_root / f"{run_id}.html",
    }
    for raw_path in (run.get("reports") or {}).values():
        if str(raw_path or "").strip():
            candidates.add(Path(str(raw_path)).resolve())

    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved.parent == report_root:
                resolved.unlink(missing_ok=True)
        except OSError:
            # Orphaned report files are harmless; the run record is the source of truth.
            pass

    if project_id:
        _invalidate(project_id)

    return {
        "ok": True,
        "run_id": run_id,
        "display_id": run.get("display_id") or f"Run {run.get('run_number', '')}",
    }


@router.post(
    "/projects/{project_id}/jobs/execute"
)
def execute(
    project_id: str,
    payload: ExecuteRequest,
):
    if not database.get_project(
        project_id
    ):
        raise HTTPException(
            404,
            "Project not found",
        )

    def work(
        progress,
    ):
        run = (
            engine.execute_suite(
                project_id,
                payload.suite_id,
                payload.priority,
                payload.limit,
                payload.test_case_ids,
                progress,
            )
        )

        run[
            "reports"
        ] = reports.write(
            run
        )

        database.save_run(
            run
        )

        _invalidate(
            project_id
        )

        return run

    return job_manager.submit(
        project_id,
        "execute_suite",
        work,
        serial_key=(
            f"execute:{project_id}"
        ),
    )


@router.get(
    "/runs/{run_id}/reports/{kind}"
)
def download_report(
    run_id: str,
    kind: str,
):
    if kind not in {
        "json",
        "html",
    }:
        raise HTTPException(
            400,
            (
                "Report kind must be "
                "json or html"
            ),
        )

    run = (
        database.get_run(
            run_id
        )
    )

    if not run:
        raise HTTPException(
            404,
            "Run not found",
        )

    report_path = str(
        (
            run.get(
                "reports"
            )
            or {}
        ).get(
            kind
        )
        or ""
    ).strip()

    if not report_path:
        raise HTTPException(
            404,
            (
                "Report is not available "
                "for this run"
            ),
        )

    path = Path(
        report_path
    ).resolve()

    report_root = (
        settings.report_dir
        .resolve()
    )

    if (
        report_root
        not in path.parents
        or not path.is_file()
    ):
        raise HTTPException(
            404,
            "Report file not found",
        )

    media_type = (
        "application/json"
        if kind == "json"
        else "text/html"
    )

    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
    )


@router.post(
    "/runs/{run_id}/baseline"
)
def set_baseline(
    run_id: str,
):
    run = (
        database.get_run(
            run_id
        )
    )

    if not run:
        raise HTTPException(
            404,
            "Run not found",
        )

    database.set_baseline(
        run[
            "project_id"
        ],
        run_id,
    )

    _invalidate(
        run[
            "project_id"
        ]
    )

    return {
        "ok": True,
        "run_id":
            run_id,
    }


@router.get(
    "/jobs/{job_id}"
)
def get_job(
    job_id: str,
):
    job = (
        database.get_job(
            job_id
        )
    )

    if not job:
        raise HTTPException(
            404,
            "Job not found",
        )

    return job