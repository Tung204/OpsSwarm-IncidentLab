from __future__ import annotations

from fastapi import FastAPI

from .simulator import router

app = FastAPI(title="OpsSwarm IncidentLab", version="1.2.0")


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "component": "incidentlab",
        "mode": "independent-target-system",
        "opsswarm_embedded": False,
    }


app.include_router(router)
