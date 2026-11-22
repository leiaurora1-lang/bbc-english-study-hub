from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


class SpeakerItem(BaseModel):
    name: str
    blurb: str = ""


class ChapterTurn(BaseModel):
    sp: str = "Speaker"
    en: str
    zh: str = ""


class ChapterItem(BaseModel):
    num: int
    time: str = "00:00"
    sec: int = 0
    title: str
    titleZh: str = ""
    speaker: str = ""
    turns: list[ChapterTurn] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class VocabCategory(BaseModel):
    key: str
    label: str


class VocabItem(BaseModel):
    word: str
    phonetic: str = ""
    cat: str = "core"
    pos: str = ""
    zh: str = ""
    ex: str = ""


class SentenceItem(BaseModel):
    id: int
    time: str = "00:00"
    sec: int = 0
    speaker: str = ""
    pattern: str = ""
    en: str
    zh: str = ""
    chunks: list[str] = Field(default_factory=list)
    imitation: str = ""


class ClozeItem(BaseModel):
    id: str
    time: str = "00:00"
    prefix: str = ""
    answer: str
    hint: str = ""
    suffix: str = ""


class QuizItem(BaseModel):
    q: str
    options: list[str] = Field(default_factory=list)
    correct: int = 0
    explanation: str = ""


class WritingPrompt(BaseModel):
    topic: str
    proPoints: list[str] = Field(default_factory=list)
    conPoints: list[str] = Field(default_factory=list)
    modelEssay: str = ""


class EpisodePayload(BaseModel):
    id: str
    youtubeId: str = ""
    videoUrl: str = ""
    mediaType: Literal["youtube", "video", "audio"] = "youtube"
    title: str
    titleZh: str = ""
    show: str = "Video English Study Hub"
    duration: str = ""
    level: str = "CEFR B2-C1"
    accent: str = ""
    sourceUrl: str = ""
    speakers: list[SpeakerItem] = Field(default_factory=list)
    summaryZh: str = ""
    vocabCategories: list[VocabCategory] = Field(default_factory=list)
    chapters: list[ChapterItem] = Field(default_factory=list)
    vocab: list[VocabItem] = Field(default_factory=list)
    sentences: list[SentenceItem] = Field(default_factory=list)
    cloze: list[ClozeItem] = Field(default_factory=list)
    quiz: list[QuizItem] = Field(default_factory=list)
    writingPrompt: Optional[WritingPrompt] = None
    isCustom: bool = True
    createdAt: Optional[str] = None


class EpisodeIndexEntry(BaseModel):
    id: str
    title: str
    titleZh: str = ""
    youtubeId: str = ""
    videoUrl: str = ""
    mediaType: Literal["youtube", "video", "audio"] = "youtube"
    duration: str = ""
    file: str
    isCustom: bool = False
    createdAt: Optional[str] = None


class ApiConfigOverride(BaseModel):
    apiKey: Optional[str] = None
    baseUrl: Optional[str] = None
    llmModel: Optional[str] = None
    asrApiKey: Optional[str] = None
    asrBaseUrl: Optional[str] = None
    asrModel: Optional[str] = None


class ImportUrlRequest(BaseModel):
    url: str
    title: Optional[str] = None
    level: Optional[str] = None
    apiConfig: Optional[ApiConfigOverride] = None
    mockMode: bool = False


class JobStatusResponse(BaseModel):
    jobId: str
    status: Literal["queued", "running", "done", "error"]
    stage: str
    progress: int
    message: str
    episodeId: Optional[str] = None
    error: Optional[str] = None
    logs: list[str] = Field(default_factory=list)
