from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .api import _invalidate, engine, reports
from .db import database
from .jobs import job_manager


router = APIRouter(prefix="/api")


class HumanActionResumeRequest(BaseModel):
    completed: bool
    note: str = Field(default="", max_length=2000)


@router.post("/runs/{run_id}/jobs/resume-human")
def resume_human_action(run_id: str, payload: HumanActionResumeRequest):
    """Resume a persisted QA run after a real human/browser/account step.

    The original execution job has already ended while the human is acting, so
    this route starts a new bounded background job. The engine keeps the same
    test case, conversation transcript, session id and sender identity.
    """

    run = database.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.get("status") != "awaiting_human":
        raise HTTPException(409, "This run is not waiting for a human action.")

    project_id = str(run.get("project_id") or "")

    def work(progress):
        try:
            resumed = engine.resume_run(
                run_id,
                payload.completed,
                payload.note.strip(),
                progress,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

        # A paused run is not final evidence. Final reports are created only
        # after execution finishes (or finishes with issues).
        if resumed.get("status") != "awaiting_human":
            resumed["reports"] = reports.write(resumed)

        database.save_run(resumed)
        _invalidate(project_id)
        return resumed

    return job_manager.submit(
        project_id,
        "resume_human_action",
        work,
        serial_key=f"execute:{project_id}",
    )

