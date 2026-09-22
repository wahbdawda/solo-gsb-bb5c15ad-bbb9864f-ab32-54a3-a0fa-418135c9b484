"""FastAPI application: REST endpoints + static single-page workbench."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .dicom_catalog import DEFAULT_PROFILE, PROFILES
from .jobs import STORE, resolve_options, run
from .residue_gate import GateBlocked
from .schemas import AnonymizationOptions

app = FastAPI(
    title="DICOM 检查匿名化工作台",
    description="上传影像 → 选择匿名化配置 → 预览逐标签变更 → 打包下载",
    version="1.0.0",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/profiles")
def list_profiles() -> dict:
    return {"default": DEFAULT_PROFILE, "profiles": PROFILES}


@app.post("/api/jobs")
async def create_job(files: list[UploadFile] = File(...)) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="未收到任何文件。")

    pairs: list[tuple[str, bytes]] = []
    total = 0
    for upload in files:
        data = await upload.read()
        total += len(data)
        if config.MAX_UPLOAD_BYTES and total > config.MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"上传总量超过限制 "
                       f"({config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB)。")
        pairs.append((upload.filename or "unnamed", data))

    job = STORE.create(pairs)
    return {
        "job_id": job.id,
        "file_count": len(pairs),
        "files": [
            {"index": i, "source_path": rel}
            for i, rel in enumerate(job.relpaths)
        ],
    }


def _prepare(job_id: str, payload: dict) -> tuple:
    try:
        job = STORE.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")

    opts_model = AnonymizationOptions(**payload)
    try:
        profile_name, options = resolve_options(
            opts_model.profile, opts_model.overrides())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    salt = opts_model.salt or config.PSEUDONYM_SALT
    profile_label = PROFILES[profile_name]["label"]
    return job, profile_name, profile_label, options, salt


def _serialize(payload: dict, job_id: str, profile_label: str,
               options: dict) -> dict:
    return {
        "job_id": job_id,
        "profile_label": profile_label,
        "options": options,
        "files": payload["files"],
        "changes": payload["changes"],
        "warnings": payload["warnings"],
        "counts": payload["counts"],
        "patients": payload["patients"],
        "gate": payload.get("gate"),
    }


@app.post("/api/jobs/{job_id}/preview")
def preview(job_id: str, payload: AnonymizationOptions) -> dict:
    job, profile_name, profile_label, options, salt = _prepare(
        job_id, payload.model_dump())
    try:
        result = run(job, profile_name, options, salt, write=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _serialize(result, job_id, profile_label, options)


@app.post("/api/jobs/{job_id}/process")
def process(job_id: str, payload: AnonymizationOptions) -> dict:
    job, profile_name, profile_label, options, salt = _prepare(
        job_id, payload.model_dump())

    # A synchronous route runs in the FastAPI threadpool, so large studies
    # do not block the event loop (preview/status/download stay responsive).
    try:
        run(job, profile_name, options, salt, write=True)
    except GateBlocked as blocked:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(blocked),
                "residue_identifier_gate": blocked.result.as_dict(),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if job.status == "error":
        raise HTTPException(status_code=400,
                            detail=job.error or "匿名化处理失败。")
    return {
        "job_id": job_id,
        "status": "done",
        "download_url": f"/api/jobs/{job_id}/download",
        "zip_filename": job.zip_filename,
        "files": job.last_result["files"] if job.last_result else [],
        "warnings": job.last_result["warnings"] if job.last_result else [],
        "counts": job.last_result["counts"] if job.last_result else {},
        "gate": job.last_result.get("gate") if job.last_result else None,
    }


@app.get("/api/jobs/{job_id}/status")
def job_status(job_id: str) -> dict:
    try:
        job = STORE.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")
    return {
        "job_id": job_id,
        "status": job.status,
        "error": job.error,
        "download_url": (f"/api/jobs/{job_id}/download"
                         if job.status == "done" else None),
        "zip_filename": job.zip_filename,
        "gate": job.last_gate,
    }


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    try:
        job = STORE.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="任务不存在或已过期。")
    if job.status == "blocked":
        raise HTTPException(
            status_code=409,
            detail="残留标识门禁未通过，ZIP 未生成；请调整匿名化选项后重新执行。")
    if job.status != "done" or not job.zip_path or \
            not job.zip_path.exists():
        raise HTTPException(status_code=409,
                            detail="ZIP 尚未生成，请先执行匿名化。")
    return FileResponse(
        job.zip_path,
        media_type="application/zip",
        filename=job.zip_filename or "anonymized.zip",
    )


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    STORE.remove(job_id)
    return {"status": "deleted"}


if STATIC_DIR.exists():
    app.mount("/static",
              StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
