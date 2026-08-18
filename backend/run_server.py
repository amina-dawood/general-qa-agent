from __future__ import annotations

import uvicorn

from qa_agent.config import settings

if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.host, port=settings.port, workers=1, reload=False, log_level=settings.log_level.lower())
