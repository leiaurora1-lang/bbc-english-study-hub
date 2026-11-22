from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from openai import OpenAI

from .schemas import (
    ApiConfigOverride,
    EpisodeIndexEntry,
    EpisodePayload,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
EPISODES_DIR = ROOT_DIR / "episodes"
UPLOADS_DIR = ROOT_DIR / "uploads"
INDEX_FILE = EPISODES_DIR / "index.json"

INDEX_LOCK = threading.Lock()
MAX_WHISPER_BYTES = 22 * 1024 * 1024
CHUNK_SECONDS = 1200  # 20 minutes per audio segment when splitting large files


@dataclass
class TranscriptSegment:
    start: float
    duration: float
    text: str


@dataclass
class MediaSourceResult:
    media_type: str  # "youtube" | "video" | "audio"
    title: str
    duration_sec: int
    channel: str
    source_url: str
    youtube_id: str = ""
    video_url: str = ""
    segments: list[TranscriptSegment] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.segments is None:
            self.segments = []


ProgressCallback = Callable[[str, int, str], None]


def slugify_id(text: str, prefix: str = "ep") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").lower()).strip("-")
    cleaned = cleaned[:36].strip("-") or prefix
    suffix = uuid.uuid4().hex[:6]
    return f"{cleaned}-{suffix}"


def format_timestamp(seconds: float | int) -> str:
    sec = max(0, int(round(seconds)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def parse_timestamp(ts: str | int | float) -> int:
    if isinstance(ts, (int, float)):
        return max(0, int(ts))
    raw = str(ts or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    parts = [p for p in raw.split(":") if p.strip().isdigit()]
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


def extract_youtube_id(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise ValueError("请输入有效的 YouTube 视频链接")
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return raw

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            return vid

    if host.endswith("youtube.com") or host == "youtube-nocookie.com":
        if parsed.path == "/watch":
            qs = parse_qs(parsed.query)
            vid = (qs.get("v") or [""])[0]
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                return vid
        for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
            if parsed.path.startswith(prefix):
                vid = parsed.path[len(prefix):].split("/")[0]
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                    return vid

    raise ValueError("无法识别的 YouTube 链接格式，请粘贴 watch?v=、youtu.be/ 或 shorts/ 链接")


def _parse_vtt_ts(ts: str) -> float:
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 2:
        return float(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    return 0.0


def parse_vtt_or_srt(content: str) -> list[TranscriptSegment]:
    lines = content.replace("\r\n", "\n").split("\n")
    segments: list[TranscriptSegment] = []
    time_re = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{1,2}:\d{2}[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{1,2}:\d{2}[.,]\d{3})"
    )

    i = 0
    last_clean = ""
    while i < len(lines):
        line = lines[i].strip()
        m = time_re.search(line)
        if not m:
            i += 1
            continue
        start = _parse_vtt_ts(m.group(1))
        end = _parse_vtt_ts(m.group(2))
        i += 1
        cue_lines: list[str] = []
        while i < len(lines) and lines[i].strip() != "":
            cue_lines.append(lines[i].strip())
            i += 1
        raw_text = " ".join(cue_lines)
        clean = re.sub(r"<[^>]+>", "", raw_text)
        clean = re.sub(r"&nbsp;", " ", clean)
        clean = re.sub(r"&amp;", "&", clean)
        clean = re.sub(r"&quot;", '"', clean)
        clean = re.sub(r"&#39;", "'", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        if not clean:
            continue
        # Deduplicate rolling YouTube auto-captions
        if clean == last_clean:
            continue
        if last_clean and clean.startswith(last_clean) and len(clean) > len(last_clean):
            delta = clean[len(last_clean):].strip()
            last_clean = clean
            if not delta:
                continue
            clean = delta
        else:
            last_clean = clean

        segments.append(
            TranscriptSegment(
                start=start,
                duration=max(0.5, end - start),
                text=clean,
            )
        )
    return segments


def probe_media_duration(file_path: Path) -> int:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        ).strip()
        return max(0, int(round(float(out))))
    except Exception:
        return 0


def detect_media_kind(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext in {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"}:
        return "audio"
    return "video"


def extract_audio_chunks(media_path: Path, work_dir: Path) -> list[tuple[Path, float]]:
    """Extract 16kHz mono mp3 and split into <=20min chunks if >22MB."""
    audio_path = work_dir / "extracted.mp3"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(media_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "64k",
        str(audio_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not audio_path.exists():
        raise RuntimeError(f"ffmpeg 抽取音轨失败: {proc.stderr[-400:]}")

    if audio_path.stat().st_size <= MAX_WHISPER_BYTES:
        return [(audio_path, 0.0)]

    chunk_pattern = work_dir / "chunk_%03d.mp3"
    split_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(audio_path),
        "-f",
        "segment",
        "-segment_time",
        str(CHUNK_SECONDS),
        "-c",
        "copy",
        str(chunk_pattern),
    ]
    proc2 = subprocess.run(split_cmd, capture_output=True, text=True, timeout=600)
    if proc2.returncode != 0:
        raise RuntimeError(f"ffmpeg 分片失败: {proc2.stderr[-400:]}")

    chunks = sorted(work_dir.glob("chunk_*.mp3"))
    if not chunks:
        return [(audio_path, 0.0)]
    return [(p, float(idx * CHUNK_SECONDS)) for idx, p in enumerate(chunks)]


def resolve_llm_client(api_config: Optional[ApiConfigOverride]) -> tuple[OpenAI, str]:
    api_key = (
        (api_config.apiKey if api_config else None)
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise ValueError(
            "未配置大模型 API Key。请在服务端 .env 设置 OPENAI_API_KEY，或在网页「导入视频 -> API 设置」中填写。"
        )
    base_url = (
        (api_config.baseUrl if api_config else None)
        or os.getenv("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).strip()
    model = (
        (api_config.llmModel if api_config else None)
        or os.getenv("STUDYHUB_LLM_MODEL")
        or "gpt-4o-mini"
    ).strip()
    return OpenAI(api_key=api_key, base_url=base_url, timeout=180.0), model


def resolve_asr_client(api_config: Optional[ApiConfigOverride]) -> tuple[OpenAI, str]:
    api_key = (
        (api_config.asrApiKey if api_config else None)
        or os.getenv("STUDYHUB_ASR_API_KEY")
        or (api_config.apiKey if api_config else None)
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise ValueError(
            "未配置语音转写（ASR）API Key。请设置 STUDYHUB_ASR_API_KEY / OPENAI_API_KEY，或在网页「API 设置」中填写。"
        )
    base_url = (
        (api_config.asrBaseUrl if api_config else None)
        or os.getenv("STUDYHUB_ASR_BASE_URL")
        or (api_config.baseUrl if api_config else None)
        or os.getenv("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).strip()
    model = (
        (api_config.asrModel if api_config else None)
        or os.getenv("STUDYHUB_ASR_MODEL")
        or "whisper-1"
    ).strip()
    return OpenAI(api_key=api_key, base_url=base_url, timeout=300.0), model


def transcribe_audio_chunks(
    chunks: list[tuple[Path, float]],
    api_config: Optional[ApiConfigOverride],
    progress_cb: Optional[ProgressCallback] = None,
) -> list[TranscriptSegment]:
    client, model = resolve_asr_client(api_config)
    all_segments: list[TranscriptSegment] = []
    total = max(1, len(chunks))

    for idx, (chunk_path, offset) in enumerate(chunks):
        if progress_cb:
            pct = 35 + int(((idx + 0.5) / total) * 25)
            progress_cb("transcribing", pct, f"正在转写音频分片 {idx + 1}/{total}...")
        with open(chunk_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model=model,
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
        segs = getattr(resp, "segments", None)
        if segs is None and isinstance(resp, dict):
            segs = resp.get("segments")
        if segs:
            for s in segs:
                s_start = float(getattr(s, "start", None) if not isinstance(s, dict) else s.get("start", 0.0) or 0.0)
                s_end = float(getattr(s, "end", None) if not isinstance(s, dict) else s.get("end", s_start + 2.0) or (s_start + 2.0))
                s_text = (getattr(s, "text", None) if not isinstance(s, dict) else s.get("text", "")) or ""
                s_text = s_text.strip()
                if s_text:
                    all_segments.append(
                        TranscriptSegment(
                            start=offset + s_start,
                            duration=max(0.5, s_end - s_start),
                            text=s_text,
                        )
                    )
        else:
            text = (getattr(resp, "text", None) if not isinstance(resp, dict) else resp.get("text", "")) or ""
            text = text.strip()
            if text:
                all_segments.append(TranscriptSegment(start=offset, duration=60.0, text=text))

    return all_segments


def fetch_youtube_source(
    url: str,
    work_dir: Path,
    api_config: Optional[ApiConfigOverride],
    mock_mode: bool = False,
    progress_cb: Optional[ProgressCallback] = None,
) -> MediaSourceResult:
    vid = extract_youtube_id(url)
    canonical_url = f"https://www.youtube.com/watch?v={vid}"

    if progress_cb:
        progress_cb("extracting", 15, f"正在读取 YouTube 视频信息 ({vid})...")

    title = f"YouTube Video {vid}"
    duration_sec = 0
    channel = "YouTube"

    # Try yt-dlp metadata first
    try:
        meta_proc = subprocess.run(
            [
                "yt-dlp",
                "--dump-single-json",
                "--no-playlist",
                "--skip-download",
                canonical_url,
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if meta_proc.returncode == 0 and meta_proc.stdout.strip():
            info = json.loads(meta_proc.stdout)
            title = info.get("title") or title
            duration_sec = int(info.get("duration") or 0)
            channel = info.get("channel") or info.get("uploader") or channel
    except Exception:
        pass

    segments: list[TranscriptSegment] = []

    # 1. Fast path: youtube-transcript-api
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            raw_list = YouTubeTranscriptApi.get_transcript(vid, languages=["en", "en-US", "en-GB"])
        else:
            api = YouTubeTranscriptApi()
            raw_list = api.fetch(vid, languages=["en", "en-US", "en-GB"]).to_raw_data()

        for item in raw_list:
            txt = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()
            if txt:
                segments.append(
                    TranscriptSegment(
                        start=float(item.get("start", 0.0)),
                        duration=float(item.get("duration", 2.0)),
                        text=txt,
                    )
                )
    except Exception:
        segments = []

    # 2. Fallback: yt-dlp subtitle download (.vtt)
    if not segments:
        if progress_cb:
            progress_cb("extracting", 25, "尝试通过 yt-dlp 提取英文字幕...")
        sub_out = str(work_dir / "yt_sub.%(ext)s")
        try:
            subprocess.run(
                [
                    "yt-dlp",
                    "--no-playlist",
                    "--skip-download",
                    "--write-sub",
                    "--write-auto-sub",
                    "--sub-lang",
                    "en.*,en",
                    "--sub-format",
                    "vtt/srt/best",
                    "-o",
                    sub_out,
                    canonical_url,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            sub_files = sorted(work_dir.glob("yt_sub*.vtt")) + sorted(work_dir.glob("yt_sub*.srt"))
            if sub_files:
                segments = parse_vtt_or_srt(sub_files[0].read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            segments = []

    # 3. Fallback: download audio & run Whisper ASR (unless mock_mode)
    if not segments and not mock_mode:
        if progress_cb:
            progress_cb("transcribing", 32, "该视频无可用英文字幕，正在下载音轨并执行语音转写...")
        audio_tpl = str(work_dir / "yt_audio.%(ext)s")
        dl_proc = subprocess.run(
            [
                "yt-dlp",
                "--no-playlist",
                "-x",
                "--audio-format",
                "mp3",
                "-o",
                audio_tpl,
                canonical_url,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        audio_files = sorted(work_dir.glob("yt_audio.*"))
        if dl_proc.returncode != 0 or not audio_files:
            raise RuntimeError(
                f"无法获取 YouTube 字幕或音轨（可能受网络/地区限制）：{dl_proc.stderr[-300:]}"
            )
        if not duration_sec:
            duration_sec = probe_media_duration(audio_files[0])
        chunks = extract_audio_chunks(audio_files[0], work_dir)
        segments = transcribe_audio_chunks(chunks, api_config, progress_cb=progress_cb)

    if not segments and mock_mode:
        segments = [
            TranscriptSegment(
                start=0.0,
                duration=12.0,
                text=f"Welcome to this English study session on {title}. Today we explore key ideas, real-world expressions, and structured arguments from the video.",
            ),
            TranscriptSegment(
                start=45.0,
                duration=15.0,
                text="As speakers unpack the core topic, notice how precise collocations and complex clauses carry both nuance and rhetorical momentum.",
            ),
        ]
        if not duration_sec:
            duration_sec = 180

    if not segments:
        raise RuntimeError("未能提取到有效英文台词或转写文本，请确认视频包含英文语音或字幕。")

    if not duration_sec and segments:
        last = segments[-1]
        duration_sec = int(round(last.start + last.duration))

    return MediaSourceResult(
        media_type="youtube",
        title=title,
        duration_sec=duration_sec,
        channel=channel,
        source_url=canonical_url,
        youtube_id=vid,
        segments=segments,
    )


def process_local_media(
    saved_path: Path,
    original_filename: str,
    api_config: Optional[ApiConfigOverride],
    mock_mode: bool = False,
    progress_cb: Optional[ProgressCallback] = None,
) -> MediaSourceResult:
    if progress_cb:
        progress_cb("extracting", 20, "正在解析本地音视频文件并抽取音轨...")

    duration_sec = probe_media_duration(saved_path)
    media_kind = detect_media_kind(saved_path)
    video_url = f"/uploads/{saved_path.name}"
    title_guess = Path(original_filename).stem.replace("_", " ").replace("-", " ").strip() or "Uploaded Video"

    if mock_mode:
        if not duration_sec:
            duration_sec = 90
        segments = [
            TranscriptSegment(
                start=0.0,
                duration=10.0,
                text="Good morning everyone, and thank you for joining today's deep dive into how technology reshapes everyday learning workflows.",
            ),
            TranscriptSegment(
                start=24.0,
                duration=14.0,
                text="Rather than memorizing isolated vocabulary lists, learners retain idioms far better when anchored to authentic video timestamps and context.",
            ),
            TranscriptSegment(
                start=52.0,
                duration=14.0,
                text="So compelling is this contextual approach that even dense policy or engineering discussions become approachable for intermediate learners.",
            ),
        ]
    else:
        with tempfile.TemporaryDirectory(prefix="studyhub_asr_") as tmp:
            work_dir = Path(tmp)
            chunks = extract_audio_chunks(saved_path, work_dir)
            segments = transcribe_audio_chunks(chunks, api_config, progress_cb=progress_cb)

    if not segments:
        raise RuntimeError("语音转写未返回有效英文文本，请确认上传的视频/音频包含清晰语音。")

    return MediaSourceResult(
        media_type=media_kind,
        title=title_guess,
        duration_sec=duration_sec,
        channel="本地上传视频" if media_kind == "video" else "本地上传音频",
        source_url=video_url,
        video_url=video_url,
        segments=segments,
    )


def merge_segments_into_blocks(
    segments: list[TranscriptSegment],
    block_seconds: int = 45,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    cur_start: Optional[float] = None
    cur_texts: list[str] = []

    for seg in segments:
        if cur_start is None:
            cur_start = seg.start
        cur_texts.append(seg.text)
        if (seg.start - cur_start) >= block_seconds and len(" ".join(cur_texts)) >= 180:
            blocks.append(
                {
                    "sec": int(round(cur_start)),
                    "time": format_timestamp(cur_start),
                    "text": " ".join(cur_texts).strip(),
                }
            )
            cur_start = None
            cur_texts = []

    if cur_texts and cur_start is not None:
        blocks.append(
            {
                "sec": int(round(cur_start)),
                "time": format_timestamp(cur_start),
                "text": " ".join(cur_texts).strip(),
            }
        )
    return blocks


def _format_blocks_for_prompt(blocks: list[dict[str, Any]], max_chars: int = 24000) -> str:
    lines: list[str] = []
    total = 0
    for b in blocks:
        line = f"[{b['time']}] {b['text']}"
        if total + len(line) > max_chars and lines:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            return json.loads(text[s : e + 1])
        raise


def _normalize_episode_dict(
    raw: dict[str, Any],
    source: MediaSourceResult,
    episode_id: str,
    custom_title: Optional[str],
    custom_level: Optional[str],
) -> EpisodePayload:
    title = (custom_title or raw.get("title") or source.title or "English Study Episode").strip()
    title_zh = (raw.get("titleZh") or title).strip()

    chapters = raw.get("chapters") or []
    norm_chapters = []
    for idx, ch in enumerate(chapters, start=1):
        ch_time = str(ch.get("time") or "00:00")
        ch_sec = ch.get("sec")
        if ch_sec is None or not isinstance(ch_sec, (int, float)):
            ch_sec = parse_timestamp(ch_time)
        else:
            ch_sec = max(0, int(ch_sec))
        turns = ch.get("turns") or []
        if not turns and ch.get("en"):
            turns = [{"sp": ch.get("speaker") or "Speaker", "en": ch["en"], "zh": ch.get("zh", "")}]
        norm_chapters.append(
            {
                "num": int(ch.get("num") or idx),
                "time": ch_time if ":" in ch_time else format_timestamp(ch_sec),
                "sec": ch_sec,
                "title": ch.get("title") or f"Section {idx}",
                "titleZh": ch.get("titleZh") or f"第 {idx} 节精讲",
                "speaker": ch.get("speaker") or "Host / Speaker",
                "turns": [
                    {
                        "sp": str(t.get("sp") or ch.get("speaker") or "Speaker"),
                        "en": str(t.get("en") or "").strip(),
                        "zh": str(t.get("zh") or "").strip(),
                    }
                    for t in turns
                    if str(t.get("en") or "").strip()
                ],
                "notes": [str(n) for n in (ch.get("notes") or []) if str(n).strip()],
            }
        )

    vocab_raw = raw.get("vocab") or []
    norm_vocab = []
    seen_cats: dict[str, str] = {}
    for v in vocab_raw:
        word = str(v.get("word") or "").strip()
        if not word:
            continue
        cat_key = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(v.get("cat") or "core").lower()).strip("_") or "core"
        if cat_key not in seen_cats:
            seen_cats[cat_key] = str(v.get("catLabel") or cat_key)
        norm_vocab.append(
            {
                "word": word,
                "phonetic": str(v.get("phonetic") or ""),
                "cat": cat_key,
                "pos": str(v.get("pos") or ""),
                "zh": str(v.get("zh") or ""),
                "ex": str(v.get("ex") or ""),
            }
        )

    raw_cats = raw.get("vocabCategories") or []
    vocab_categories = []
    if isinstance(raw_cats, list) and raw_cats:
        for c in raw_cats:
            if isinstance(c, dict) and c.get("key") and c.get("label"):
                k = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(c["key"]).lower()).strip("_")
                if k:
                    vocab_categories.append({"key": k, "label": str(c["label"])})
    if not vocab_categories and seen_cats:
        default_labels = {
            "core": "核心高频词",
            "idiom": "地道习语表达",
            "collocation": "实用搭配短语",
            "topic": "主题专业词汇",
        }
        for k, fallback_lbl in seen_cats.items():
            vocab_categories.append({"key": k, "label": default_labels.get(k, fallback_lbl)})

    norm_sentences = []
    for idx, s in enumerate(raw.get("sentences") or [], start=1):
        en = str(s.get("en") or "").strip()
        if not en:
            continue
        s_time = str(s.get("time") or "00:00")
        s_sec = s.get("sec")
        s_sec = parse_timestamp(s_time) if not isinstance(s_sec, (int, float)) else max(0, int(s_sec))
        norm_sentences.append(
            {
                "id": int(s.get("id") or idx),
                "time": s_time if ":" in s_time else format_timestamp(s_sec),
                "sec": s_sec,
                "speaker": str(s.get("speaker") or ""),
                "pattern": str(s.get("pattern") or "高级句型结构"),
                "en": en,
                "zh": str(s.get("zh") or ""),
                "chunks": [str(c) for c in (s.get("chunks") or []) if str(c).strip()],
                "imitation": str(s.get("imitation") or ""),
            }
        )

    norm_cloze = []
    for idx, c in enumerate(raw.get("cloze") or [], start=1):
        ans = str(c.get("answer") or "").strip()
        if not ans:
            continue
        norm_cloze.append(
            {
                "id": str(c.get("id") or f"c{idx}"),
                "time": str(c.get("time") or "00:00"),
                "prefix": str(c.get("prefix") or ""),
                "answer": ans,
                "hint": str(c.get("hint") or f"{ans[0]}___"),
                "suffix": str(c.get("suffix") or ""),
            }
        )

    norm_quiz = []
    for q in raw.get("quiz") or []:
        opts = [str(o) for o in (q.get("options") or []) if str(o).strip()]
        if not opts or not q.get("q"):
            continue
        corr = int(q.get("correct") or 0)
        if corr < 0 or corr >= len(opts):
            corr = 0
        norm_quiz.append(
            {
                "q": str(q["q"]),
                "options": opts,
                "correct": corr,
                "explanation": str(q.get("explanation") or ""),
            }
        )

    wp = raw.get("writingPrompt")
    if isinstance(wp, dict) and wp.get("topic"):
        writing_prompt = {
            "topic": str(wp["topic"]),
            "proPoints": [str(p) for p in (wp.get("proPoints") or []) if str(p).strip()],
            "conPoints": [str(p) for p in (wp.get("conPoints") or []) if str(p).strip()],
            "modelEssay": str(wp.get("modelEssay") or ""),
        }
    else:
        writing_prompt = None

    payload_dict = {
        "id": episode_id,
        "youtubeId": source.youtube_id,
        "videoUrl": source.video_url,
        "mediaType": source.media_type,
        "title": title,
        "titleZh": title_zh,
        "show": str(raw.get("show") or source.channel or "Video English Study Hub"),
        "duration": str(raw.get("duration") or format_timestamp(source.duration_sec)),
        "level": str(custom_level or raw.get("level") or "CEFR B2-C1"),
        "accent": str(raw.get("accent") or "原声英文"),
        "sourceUrl": source.source_url,
        "speakers": [
            {"name": str(sp.get("name") or "Speaker"), "blurb": str(sp.get("blurb") or "")}
            for sp in (raw.get("speakers") or [])
            if isinstance(sp, dict)
        ]
        or [{"name": source.channel or "Main Speaker", "blurb": "视频主讲人 / 访谈嘉宾"}],
        "summaryZh": str(raw.get("summaryZh") or f"围绕《{title}》生成的双语精听与外刊精读讲义。"),
        "vocabCategories": vocab_categories,
        "chapters": norm_chapters,
        "vocab": norm_vocab,
        "sentences": norm_sentences,
        "cloze": norm_cloze,
        "quiz": norm_quiz,
        "writingPrompt": writing_prompt,
        "isCustom": True,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    return EpisodePayload.model_validate(payload_dict)


def generate_mock_episode(
    source: MediaSourceResult,
    episode_id: str,
    custom_title: Optional[str] = None,
    custom_level: Optional[str] = None,
) -> EpisodePayload:
    blocks = merge_segments_into_blocks(source.segments, block_seconds=30)
    if not blocks:
        blocks = [{"sec": 0, "time": "00:00", "text": "Welcome to this English video study session."}]

    chapters = []
    for idx, b in enumerate(blocks[:6], start=1):
        chapters.append(
            {
                "num": idx,
                "time": b["time"],
                "sec": b["sec"],
                "title": f"Part {idx}: Key Excerpt ({b['time']})",
                "titleZh": f"第 {idx} 段原声精讲（{b['time']}）",
                "speaker": source.channel or "Speaker",
                "turns": [
                    {
                        "sp": source.channel or "Speaker",
                        "en": b["text"],
                        "zh": f"【中文导读】本段从 {b['time']} 开始，围绕核心主题展开论述与表达示范。",
                    }
                ],
                "notes": ["authentic context (地道语境)", "key collocation (核心搭配)"],
            }
        )

    sample_en = blocks[0]["text"]
    raw = {
        "title": custom_title or source.title,
        "titleZh": f"视频精学：{custom_title or source.title}",
        "show": source.channel or "Video Study Hub",
        "duration": format_timestamp(source.duration_sec or 120),
        "level": custom_level or "CEFR B2-C1",
        "accent": "原声英文",
        "speakers": [{"name": source.channel or "Primary Speaker", "blurb": "视频主讲人，结合真实语境输出核心观点。"}],
        "summaryZh": f"本学习包由视频《{custom_title or source.title}》自动提取字幕与时间轴生成，包含分段双语精讲、主题词库、长难句拆解及配套测验。",
        "vocabCategories": [
            {"key": "core", "label": "核心高频词"},
            {"key": "collocation", "label": "地道搭配"},
            {"key": "idiom", "label": "表达习语"},
        ],
        "chapters": chapters,
        "vocab": [
            {
                "word": "unpack",
                "phonetic": "/ˌʌnˈpæk/",
                "cat": "core",
                "pos": "v.",
                "zh": "深入剖析；拆解说明",
                "ex": f"{sample_en[:120]} ({blocks[0]['time']})",
            },
            {
                "word": "rhetorical momentum",
                "phonetic": "/rɪˈtɒrɪkəl məʊˈmentəm/",
                "cat": "collocation",
                "pos": "n. phr.",
                "zh": "论述气势；修辞推进力",
                "ex": "Precise collocations carry both nuance and rhetorical momentum.",
            },
            {
                "word": "anchor to",
                "phonetic": "/ˈæŋkər tuː/",
                "cat": "idiom",
                "pos": "phr.",
                "zh": "锚定于；扎根于（某语境）",
                "ex": "Learners retain idioms better when anchored to authentic video timestamps.",
            },
        ],
        "sentences": [
            {
                "id": 1,
                "time": blocks[0]["time"],
                "sec": blocks[0]["sec"],
                "speaker": source.channel or "Speaker",
                "pattern": "主从复合句 + 语境信息拓展",
                "en": sample_en,
                "zh": "结合真实视频时间轴进行精听与拆解，有助于把生词和句型真正内化为主动表达。",
                "chunks": [
                    "核心主干: 提取视频原始语音切片并绑定精确时间码",
                    "学习策略: 先盲听主干，再核对中英对照与重点搭配",
                ],
                "imitation": "仿写示范: Rather than memorizing isolated words, learners internalize expressions by anchoring them to real-world clips.",
            }
        ],
        "cloze": [
            {
                "id": "c1",
                "time": blocks[0]["time"],
                "prefix": "Learners retain idioms far better when ",
                "answer": "anchored",
                "hint": "a_______ (锚定/绑定)",
                "suffix": " to authentic video timestamps and context.",
            }
        ],
        "quiz": [
            {
                "q": f"1. What is the main focus of '{custom_title or source.title}'?",
                "options": [
                    "A. Exploring key ideas through authentic spoken context",
                    "B. Memorizing unrelated dictionary definitions in isolation",
                    "C. Skipping audio comprehension altogether",
                    "D. Avoiding structured speaking practice",
                ],
                "correct": 0,
                "explanation": f"原文起始位置 {blocks[0]['time']} 强调结合真实视频语境理解核心表达。",
            }
        ],
        "writingPrompt": {
            "topic": f"Reflect on the central argument of \"{custom_title or source.title}\" and explain how its key expressions apply to real-world communication.",
            "proPoints": [
                "Contextual exposure helps learners <code>anchor</code> vocabulary to real scenarios.",
                "Breaking down long sentences reveals how speakers build <code>rhetorical momentum</code>.",
            ],
            "conPoints": [
                "Without deliberate review, passive watching rarely converts input into active output.",
                "Combining listening cloze with shadowing bridges comprehension and speaking.",
            ],
            "modelEssay": "Watching authentic English videos becomes exponentially more effective when paired with structured breakdown. Instead of treating clips as passive entertainment, anchoring new collocations to exact timestamps allows learners to revisit nuance and pronunciation on demand. Ultimately, combining active listening, syntactic analysis, and short imitation turns every video into a reusable personal curriculum.",
        },
    }
    return _normalize_episode_dict(raw, source, episode_id, custom_title, custom_level)


def generate_study_payload(
    source: MediaSourceResult,
    episode_id: str,
    api_config: Optional[ApiConfigOverride],
    custom_title: Optional[str] = None,
    custom_level: Optional[str] = None,
    mock_mode: bool = False,
    progress_cb: Optional[ProgressCallback] = None,
) -> EpisodePayload:
    if mock_mode:
        if progress_cb:
            progress_cb("generating", 80, "正在生成全站学习讲义（演示模式）...")
        return generate_mock_episode(source, episode_id, custom_title, custom_level)

    client, model = resolve_llm_client(api_config)
    blocks = merge_segments_into_blocks(source.segments, block_seconds=45)
    transcript_text = _format_blocks_for_prompt(blocks, max_chars=24000)

    if progress_cb:
        progress_cb("generating", 68, f"正在调用 {model} 生成双语精讲、词库与练习...")

    system_prompt = (
        "You are an expert bilingual (English + Simplified Chinese) English learning curriculum designer. "
        "Given a video's metadata and timestamped English transcript, return a SINGLE valid JSON object "
        "matching the exact schema requested. Use natural, accurate Simplified Chinese explanations. "
        "All timestamps (time as 'MM:SS' and sec as integer seconds) MUST come from the provided transcript."
    )

    user_prompt = f"""
视频标题: {custom_title or source.title}
频道/来源: {source.channel}
总时长: {format_timestamp(source.duration_sec)}
目标学习难度: {custom_level or "CEFR B2-C1"}

带时间戳英文原声文稿：
{transcript_text}

请基于以上文稿生成完整 JSON 对象，必须严格包含以下字段：
{{
  "title": "英文标题",
  "titleZh": "精准中文标题",
  "show": "节目或频道名称",
  "duration": "{format_timestamp(source.duration_sec)}",
  "level": "{custom_level or 'CEFR B2-C1'}",
  "accent": "如 美音 / 英音 / 多口音访谈",
  "speakers": [{{"name": "说话人名称", "blurb": "中文简介（20-40字）"}}],
  "summaryZh": "中文内容总览（80-150字）",
  "vocabCategories": [
    {{"key": "topic", "label": "主题核心词"}},
    {{"key": "collocation", "label": "地道搭配"}},
    {{"key": "idiom", "label": "习惯表达"}}
  ],
  "chapters": [
    {{
      "num": 1,
      "time": "MM:SS",
      "sec": 0,
      "title": "English chapter title",
      "titleZh": "中文小节标题",
      "speaker": "Speaker name",
      "turns": [
        {{"sp": "Speaker", "en": "保留原汁原味英文原文片段", "zh": "地道准确的中文翻译"}}
      ],
      "notes": ["key phrase (中文释义)"]
    }}
  ],
  "vocab": [
    {{
      "word": "phrase or word from transcript",
      "phonetic": "/IPA/",
      "cat": "must match one of vocabCategories[].key",
      "pos": "v. / n. / phr. / adj.",
      "zh": "中文释义",
      "ex": "原声例句 (MM:SS)"
    }}
  ],
  "sentences": [
    {{
      "id": 1,
      "time": "MM:SS",
      "sec": 0,
      "speaker": "Speaker",
      "pattern": "语法/修辞句型标签",
      "en": "原声精彩长难句",
      "zh": "准确中文翻译",
      "chunks": ["结构拆解1: ...", "结构拆解2: ..."],
      "imitation": "仿写示范: ..."
    }}
  ],
  "cloze": [
    {{
      "id": "c1",
      "time": "MM:SS",
      "prefix": "挖空前半句 ",
      "answer": "single target word",
      "hint": "首字母提示 (中文意思)",
      "suffix": " 挖空后半句"
    }}
  ],
  "quiz": [
    {{
      "q": "1. 英文理解题干",
      "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
      "correct": 0,
      "explanation": "中文解析与原文时间点定位"
    }}
  ],
  "writingPrompt": {{
    "topic": "围绕本视频主题的英文思考/辩论题目",
    "proPoints": ["观点支架1（可用 <code>词组</code> 标出关键词）"],
    "conPoints": ["观点支架2（可用 <code>词组</code> 标出关键词）"],
    "modelEssay": "约 110-140 词的高分英文参考段落，自然融入本集核心词汇"
  }}
}}

数量要求：
- chapters: 4 到 10 个章节（覆盖全片时间线，每章 1-3 轮 turns，不要遗漏关键段落）
- vocab: 12 到 24 个高价值词汇/短语，均匀分布到 3-4 个 vocabCategories 中
- sentences: 4 到 8 句长难句或地道金句
- cloze: 4 到 6 道精听完形填空（answer 为单个单词，大小写自然）
- quiz: 4 到 5 道细节与观点理解题（correct 为 0-3 整数下标）
仅输出 JSON，不要输出任何额外解释文字。
"""

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    content = resp.choices[0].message.content or "{}"
    raw_obj = _extract_json_object(content)

    if progress_cb:
        progress_cb("generating", 90, "正在校验结构并写入剧集仓库...")

    return _normalize_episode_dict(raw_obj, source, episode_id, custom_title, custom_level)


def save_episode_atomic(payload: EpisodePayload) -> EpisodeIndexEntry:
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    ep_file = f"{payload.id}.json"
    ep_path = EPISODES_DIR / ep_file

    tmp_ep = EPISODES_DIR / f".{ep_file}.{uuid.uuid4().hex}.tmp"
    tmp_ep.write_text(
        json.dumps(payload.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_ep, ep_path)

    entry = EpisodeIndexEntry(
        id=payload.id,
        title=payload.title,
        titleZh=payload.titleZh,
        youtubeId=payload.youtubeId,
        videoUrl=payload.videoUrl,
        mediaType=payload.mediaType,
        duration=payload.duration,
        file=ep_file,
        isCustom=payload.isCustom,
        createdAt=payload.createdAt,
    )

    with INDEX_LOCK:
        existing: list[dict[str, Any]] = []
        if INDEX_FILE.exists():
            try:
                existing = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        filtered = [item for item in existing if item.get("id") != payload.id]
        # Place newly imported episode at the front so user sees it right away
        updated = [entry.model_dump()] + filtered
        tmp_idx = EPISODES_DIR / f".index.{uuid.uuid4().hex}.tmp"
        tmp_idx.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_idx, INDEX_FILE)

    return entry


def delete_custom_episode(episode_id: str) -> None:
    if episode_id == "nvidia-scandal":
        raise PermissionError("内置示例剧集不允许删除")

    with INDEX_LOCK:
        if not INDEX_FILE.exists():
            raise FileNotFoundError("未找到剧集索引")
        existing: list[dict[str, Any]] = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        target = next((item for item in existing if item.get("id") == episode_id), None)
        if not target:
            raise FileNotFoundError(f"剧集 {episode_id} 不存在")
        if target.get("isCustom") is False:
            raise PermissionError("内置剧集不允许删除")

        remaining = [item for item in existing if item.get("id") != episode_id]
        tmp_idx = EPISODES_DIR / f".index.{uuid.uuid4().hex}.tmp"
        tmp_idx.write_text(
            json.dumps(remaining, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_idx, INDEX_FILE)

        ep_file = EPISODES_DIR / (target.get("file") or f"{episode_id}.json")
        if ep_file.exists():
            ep_file.unlink()

        video_url = target.get("videoUrl") or ""
        if video_url.startswith("/uploads/"):
            rel_name = Path(video_url).name
            up_file = UPLOADS_DIR / rel_name
            if up_file.exists():
                up_file.unlink()
