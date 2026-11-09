"""MiniTask FastAPI application and web interface."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import database
from app.models import Task, TaskCreate, TaskUpdate


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.initialize()
    yield


app = FastAPI(
    title="MiniTask API",
    description="A small application for testing autonomous code changes with PatchPilot.",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static"), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/tasks", response_model=list[Task])
def list_tasks() -> list[dict]:
    return database.list_tasks()


@app.post("/api/tasks", response_model=Task, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate) -> dict:
    return database.create_task(
        title=payload.title,
        description=payload.description,
        status=payload.status.value,
    )


@app.patch("/api/tasks/{task_id}", response_model=Task)
def patch_task(task_id: int, payload: TaskUpdate) -> dict:
    changes = payload.model_dump(exclude_unset=True)
    if "status" in changes:
        changes["status"] = changes["status"].value
    task = database.update_task(task_id, changes)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.delete("/api/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_task(task_id: int) -> Response:
    if not database.delete_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
