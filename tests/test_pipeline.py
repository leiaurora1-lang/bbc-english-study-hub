from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.pipeline import (
    EPISODES_DIR,
    INDEX_FILE,
    UPLOADS_DIR,
    extract_audio_chunks,
    extract_youtube_id,
    parse_vtt_or_srt,
)
from server.schemas import EpisodePayload


def test_extract_youtube_id_variants():
    assert extract_youtube_id("https://www.youtube.com/watch?v=RiMjEiqpuB0") == "RiMjEiqpuB0"
    assert extract_youtube_id("https://youtu.be/RiMjEiqpuB0?t=42") == "RiMjEiqpuB0"
    assert extract_youtube_id("https://www.youtube.com/shorts/RiMjEiqpuB0") == "RiMjEiqpuB0"
    assert extract_youtube_id("https://www.youtube.com/embed/RiMjEiqpuB0") == "RiMjEiqpuB0"
    with pytest.raises(ValueError):
        extract_youtube_id("https://example.com/not-youtube")


def test_parse_vtt_dedupes_rolling_captions():
    vtt = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000
<c>Hello</c> world

00:00:03.000 --> 00:00:05.000
Hello world and welcome back

00:00:05.000 --> 00:00:07.000
Today we discuss chips
"""
    segs = parse_vtt_or_srt(vtt)
    assert len(segs) == 3
    assert segs[0].text == "Hello world"
    assert segs[1].text == "and welcome back"
    assert segs[2].text == "Today we discuss chips"


def test_existing_nvidia_episode_validates_schema():
    raw = json.loads((EPISODES_DIR / "nvidia-scandal.json").read_text(encoding="utf-8"))
    ep = EpisodePayload.model_validate(raw)
    assert ep.id == "nvidia-scandal"
    assert len(ep.chapters) > 0
    assert len(ep.vocab) > 0


def test_extract_audio_chunks_with_ffmpeg(tmp_path: Path):
    sample_mp4 = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=2",
            "-shortest",
            str(sample_mp4),
        ],
        check=True,
        capture_output=True,
    )
    work_dir = tmp_path / "out"
    work_dir.mkdir()
    chunks = extract_audio_chunks(sample_mp4, work_dir)
    assert len(chunks) == 1
    assert chunks[0][0].exists()
    assert chunks[0][1] == 0.0


def test_e2e_file_upload_mock_and_cleanup(tmp_path: Path):
    sample_mp4 = tmp_path / "demo_talk.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=520:duration=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=320x180:d=2",
            "-shortest",
            str(sample_mp4),
        ],
        check=True,
        capture_output=True,
    )

    client = TestClient(app)

    # 1. Health & static index
    r_health = client.get("/api/health")
    assert r_health.status_code == 200
    assert r_health.json()["ok"] is True
    assert client.get("/").status_code == 200

    # 2. Cannot delete built-in episode
    r_del_builtin = client.delete("/api/episodes/nvidia-scandal")
    assert r_del_builtin.status_code == 403

    # 3. Upload local video in mockMode
    with open(sample_mp4, "rb") as f:
        r_imp = client.post(
            "/api/episodes/import-file",
            files={"file": ("demo_talk.mp4", f, "video/mp4")},
            data={"title": "Demo AI Talk", "level": "CEFR B2-C1", "mockMode": "true"},
        )
    assert r_imp.status_code == 200
    job_id = r_imp.json()["jobId"]

    # 4. Poll until done
    job = None
    for _ in range(30):
        r_job = client.get(f"/api/jobs/{job_id}")
        assert r_job.status_code == 200
        job = r_job.json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)

    assert job is not None
    assert job["status"] == "done", job
    ep_id = job["episodeId"]
    assert ep_id

    # 5. Verify generated episode & uploaded video are served
    r_idx = client.get("/episodes/index.json")
    assert r_idx.status_code == 200
    index_items = r_idx.json()
    assert any(item["id"] == ep_id for item in index_items)

    r_ep = client.get(f"/episodes/{ep_id}.json")
    assert r_ep.status_code == 200
    ep_data = EpisodePayload.model_validate(r_ep.json())
    assert ep_data.mediaType == "video"
    assert ep_data.videoUrl.startswith("/uploads/")
    assert len(ep_data.vocabCategories) > 0
    assert ep_data.writingPrompt is not None

    r_vid = client.get(ep_data.videoUrl)
    assert r_vid.status_code == 200
    uploaded_file = UPLOADS_DIR / Path(ep_data.videoUrl).name
    assert uploaded_file.exists()

    # 6. Cleanup custom episode
    r_del = client.delete(f"/api/episodes/{ep_id}")
    assert r_del.status_code == 200
    assert not (EPISODES_DIR / f"{ep_id}.json").exists()
    assert not uploaded_file.exists()
    index_after = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    assert not any(item["id"] == ep_id for item in index_after)


def test_e2e_youtube_url_mock_and_cleanup(monkeypatch):
    from server import app as app_mod
    from server.pipeline import MediaSourceResult, TranscriptSegment

    def fake_fetch(url, work_dir, api_config, mock_mode=False, progress_cb=None):
        if progress_cb:
            progress_cb("extracting", 25, "mock fetching youtube")
        return MediaSourceResult(
            media_type="youtube",
            title="Mocked YouTube Talk",
            duration_sec=145,
            channel="Mock Channel",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            youtube_id="dQw4w9WgXcQ",
            segments=[
                TranscriptSegment(
                    start=0.0,
                    duration=12.0,
                    text="Hello and welcome to our in-depth discussion on building resilient software systems that adapt smoothly across different media pipelines and browser environments.",
                ),
                TranscriptSegment(
                    start=35.0,
                    duration=14.0,
                    text="In the first section we examine how modular data schemas decouple presentation from content ingestion.",
                ),
                TranscriptSegment(
                    start=75.0,
                    duration=12.0,
                    text="Notice how modular architecture lets us swap media sources effortlessly while preserving local study progress.",
                ),
            ],
        )

    monkeypatch.setattr(app_mod, "fetch_youtube_source", fake_fetch)
    client = TestClient(app)

    r_imp = client.post(
        "/api/episodes/import-url",
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "mockMode": True},
    )
    assert r_imp.status_code == 200
    job_id = r_imp.json()["jobId"]

    job = None
    for _ in range(30):
        r_job = client.get(f"/api/jobs/{job_id}")
        assert r_job.status_code == 200
        job = r_job.json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)

    assert job is not None and job["status"] == "done", job
    ep_id = job["episodeId"]

    try:
        r_ep = client.get(f"/episodes/{ep_id}.json")
        assert r_ep.status_code == 200
        ep_data = EpisodePayload.model_validate(r_ep.json())
        assert ep_data.youtubeId == "dQw4w9WgXcQ"
        assert ep_data.mediaType == "youtube"
        assert len(ep_data.chapters) >= 2
    finally:
        r_del = client.delete(f"/api/episodes/{ep_id}")
        assert r_del.status_code == 200
        assert not (EPISODES_DIR / f"{ep_id}.json").exists()

