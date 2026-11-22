from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .pipeline import (
    EPISODES_DIR,
    ROOT_DIR,
    UPLOADS_DIR,
    delete_custom_episode,
    fetch_youtube_source,
    generate_study_payload,
    process_local_media,
    save_episode_atomic,
    slugify_id,
)
from .schemas import ApiConfigOverride, ImportUrlRequest, JobStatusResponse

load_dotenv(ROOT_DIR / ".env")

EPISODES_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_MEDIA_EXTS = {
    ".mp4",
    ".webm",
    ".mov",
    ".mkv",
    ".m4v",
    ".mp3",
    ".m4a",
    ".wav",
    ".ogg",
    ".flac",
    ".aac",
}

app = FastAPI(title="Video English Study Hub API", version="1.0.0")

EXECUTOR = ThreadPoolExecutor(max_workers=2)
JOBS_LOCK = threading.Lock()
JOBS: dict[str, dict[str, Any]] = {}


def _create_job(initial_msg: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "jobId": job_id,
            "status": "queued",
            "stage": "queued",
            "progress": 5,
            "message": initial_msg,
            "episodeId": None,
            "error": None,
            "logs": [initial_msg],
        }
    return job_id


def _update_job(
    job_id: str,
    *,
    status: Optional[str] = None,
    stage: Optional[str] = None,
    progress: Optional[int] = None,
    message: Optional[str] = None,
    episode_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        if status is not None:
            job["status"] = status
        if stage is not None:
            job["stage"] = stage
        if progress is not None:
            job["progress"] = progress
        if message is not None:
            job["message"] = message
            if not job["logs"] or job["logs"][-1] != message:
                job["logs"].append(message)
        if episode_id is not None:
            job["episodeId"] = episode_id
        if error is not None:
            job["error"] = error


def _run_url_job(job_id: str, req: ImportUrlRequest) -> None:
    def progress_cb(stage: str, pct: int, msg: str) -> None:
        _update_job(job_id, status="running", stage=stage, progress=pct, message=msg)

    try:
        _update_job(job_id, status="running", stage="extracting", progress=10, message="开始解析 YouTube 链接...")
        with tempfile.TemporaryDirectory(prefix="studyhub_yt_") as tmp:
            source = fetch_youtube_source(
                req.url,
                Path(tmp),
                req.apiConfig,
                mock_mode=req.mockMode,
                progress_cb=progress_cb,
            )
            ep_id = slugify_id(req.title or source.title, prefix="yt")
            payload = generate_study_payload(
                source,
                ep_id,
                req.apiConfig,
                custom_title=req.title,
                custom_level=req.level,
                mock_mode=req.mockMode,
                progress_cb=progress_cb,
            )
            save_episode_atomic(payload)
            _update_job(
                job_id,
                status="done",
                stage="done",
                progress=100,
                message="讲义生成完成，正在切换整站内容！",
                episode_id=payload.id,
            )
    except Exception as exc:
        _update_job(
            job_id,
            status="error",
            stage="error",
            progress=100,
            message=f"处理失败：{exc}",
            error=str(exc),
        )


def _run_file_job(
    job_id: str,
    saved_path: Path,
    original_filename: str,
    custom_title: Optional[str],
    custom_level: Optional[str],
    api_config: Optional[ApiConfigOverride],
    mock_mode: bool,
) -> None:
    def progress_cb(stage: str, pct: int, msg: str) -> None:
        _update_job(job_id, status="running", stage=stage, progress=pct, message=msg)

    try:
        _update_job(job_id, status="running", stage="extracting", progress=12, message="文件上传完成，开始抽取音轨...")
        source = process_local_media(
            saved_path,
            original_filename,
            api_config,
            mock_mode=mock_mode,
            progress_cb=progress_cb,
        )
        ep_id = slugify_id(custom_title or source.title, prefix="media")
        payload = generate_study_payload(
            source,
            ep_id,
            api_config,
            custom_title=custom_title,
            custom_level=custom_level,
            mock_mode=mock_mode,
            progress_cb=progress_cb,
        )
        save_episode_atomic(payload)
        _update_job(
            job_id,
            status="done",
            stage="done",
            progress=100,
            message="讲义生成完成，正在切换整站内容！",
            episode_id=payload.id,
        )
    except Exception as exc:
        _update_job(
            job_id,
            status="error",
            stage="error",
            progress=100,
            message=f"处理失败：{exc}",
            error=str(exc),
        )


@app.get("/api/health")
def health_check() -> dict[str, Any]:
    has_llm_key = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    has_asr_key = bool((os.getenv("STUDYHUB_ASR_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip())
    return {
        "ok": True,
        "hasLlmKey": has_llm_key,
        "hasAsrKey": has_asr_key,
        "llmModel": os.getenv("STUDYHUB_LLM_MODEL") or "gpt-4o-mini",
        "asrModel": os.getenv("STUDYHUB_ASR_MODEL") or "whisper-1",
    }


@app.post("/api/episodes/import-url")
def import_url(req: ImportUrlRequest) -> dict[str, str]:
    if not req.url or not req.url.strip():
        raise HTTPException(status_code=400, detail="请提供有效的 YouTube 链接")
    job_id = _create_job("已接收 YouTube 导入任务，准备解析...")
    EXECUTOR.submit(_run_url_job, job_id, req)
    return {"jobId": job_id}


@app.post("/api/episodes/import-file")
async def import_file(
    file: UploadFile = File(...),
    title: Optional[str] = Form(default=None),
    level: Optional[str] = Form(default=None),
    apiConfigJson: Optional[str] = Form(default=None),
    mockMode: bool = Form(default=False),
) -> dict[str, str]:
    filename = file.filename or "upload.mp4"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_MEDIA_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的媒体格式 {ext}，支持：{', '.join(sorted(ALLOWED_MEDIA_EXTS))}",
        )

    api_config: Optional[ApiConfigOverride] = None
    if apiConfigJson and apiConfigJson.strip():
        try:
            api_config = ApiConfigOverride.model_validate(json.loads(apiConfigJson))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"apiConfigJson 格式无效：{exc}") from exc

    save_name = f"{uuid.uuid4().hex[:12]}{ext}"
    saved_path = UPLOADS_DIR / save_name
    with open(saved_path, "wb") as out:
        shutil.copyfileobj(file.file, out)

    job_id = _create_job(f"已上传 {filename}，准备处理...")
    EXECUTOR.submit(
        _run_file_job,
        job_id,
        saved_path,
        filename,
        title.strip() if title else None,
        level.strip() if level else None,
        api_config,
        mockMode,
    )
    return {"jobId": job_id}


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str) -> JobStatusResponse:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        return JobStatusResponse(**job)


@app.delete("/api/episodes/{episode_id}")
def remove_episode(episode_id: str) -> dict[str, Any]:
    try:
        delete_custom_episode(episode_id)
        return {"ok": True, "deleted": episode_id}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/")
def serve_index() -> FileResponse:
    return FileResponse(ROOT_DIR / "index.html")


# Mount static directories cleanly without shadowing /api routes
app.mount("/assets", StaticFiles(directory=str(ROOT_DIR / "assets")), name="assets")
app.mount("/episodes", StaticFiles(directory=str(EPISODES_DIR)), name="episodes")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
