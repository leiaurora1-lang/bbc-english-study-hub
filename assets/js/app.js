/**
 * Video English Study Hub — reusable multi-episode & dynamic import engine
 * Content lives in /episodes/*.json ; backend API lives at /api/*
 */

let currentEpisode = null;
let currentEpisodeMetaList = [];
let CHAPTERS = [];
let VOCAB_LIST = [];
let SENTENCES = [];
let CLOZE_ITEMS = [];
let QUIZ_ITEMS = [];

let fcIndex = 0;
let fcFlipped = false;
let flashcardSource = "builtin";
let masteredSet = new Set();
let notebookItems = [];
let pendingSelection = null;

let currentAudio = null;
let cachedVoices = [];
let speakTimer = null;

let importMode = "url"; // "url" | "file"
let selectedUploadFile = null;
let activeJobTimer = null;

const EMBEDDED_INDEX = typeof window !== "undefined" && window.EMBEDDED_EPISODE_INDEX
  ? window.EMBEDDED_EPISODE_INDEX
  : null;
const EMBEDDED_EPISODES = typeof window !== "undefined" && window.EMBEDDED_EPISODES
  ? window.EMBEDDED_EPISODES
  : null;

const LEGACY_CAT_NAMES = {
  ai: "AI 与半导体产业",
  geo: "地缘与出口管制",
  idiom: "地道新闻习语",
  press: "调查与公关辞令",
  core: "核心高频词",
  collocation: "地道搭配",
  topic: "主题核心词"
};

const DEFAULT_WRITING_PROMPT = {
  topic: "Should advanced AI chips be strictly restricted from export, or does open ecosystem adoption better serve technological leadership?",
  proPoints: [
    "Proponents of open trade proffer the argument that global AI competition is not a <code>zero-sum game</code>.",
    "Getting global developers hooked on the <code>US tech stack</code> prevents rivals from accelerating their own <code>indigenous capabilities</code>."
  ],
  conPoints: [
    "Frontier GPUs sit at the <code>infrastructure layer</code> of both commercial and military AI.",
    "Even if export controls cannot permanently halt advancement, <code>thwarting</code> a competitor's progress by 6 to 12 months is strategically meaningful."
  ],
  modelEssay: "The debate over exporting cutting-edge semiconductors sits at the epicenter of modern geopolitics. Industry leaders like Jensen Huang have long proffered the argument that technological competition is not a zero-sum game; rather, encouraging global reliance on the US tech stack disincentivizes rivals from building their own indigenous capabilities. However, a bipartisan consensus within the Washington national security apparatus contends that frontier GPUs are dual-use assets that directly impact the military domain. While export controls on the books may not permanently prevent a well-funded competitor's evolution, thwarting their access to top-tier hardware for even six to twelve months provides a decisive strategic buffer. Ultimately, as hawkish sentiment prevails, tech giants find it increasingly difficult to decouple commercial ambition from national security ramifications."
};

function storageKey(suffix) {
  const id = (currentEpisode && currentEpisode.id) || "default";
  return `studyhub:${id}:${suffix}`;
}

function getQueryEpisodeId() {
  try {
    return new URLSearchParams(window.location.search).get("episode") || "";
  } catch (_) {
    return "";
  }
}

async function fetchJson(url) {
  const sep = url.includes("?") ? "&" : "?";
  const res = await fetch(`${url}${sep}_ts=${Date.now()}`);
  if (!res.ok) throw new Error(`Failed to load ${url}: ${res.status}`);
  return res.json();
}

async function loadEpisodeIndex() {
  if (EMBEDDED_INDEX && EMBEDDED_INDEX.length) return EMBEDDED_INDEX;
  return fetchJson("episodes/index.json");
}

async function loadEpisodePayload(meta) {
  if (EMBEDDED_EPISODES && EMBEDDED_EPISODES[meta.id]) {
    return EMBEDDED_EPISODES[meta.id];
  }
  const file = meta.file || `${meta.id}.json`;
  return fetchJson(`episodes/${file}`);
}

function migrateLegacyNvidiaStorage() {
  if (!currentEpisode || currentEpisode.id !== "nvidia-scandal") return;
  const pairs = [
    ["bbc_nvidia_notebook", "notebook"],
    ["bbc_nvidia_mastered", "mastered"],
    ["bbc_nvidia_essay", "essay"]
  ];
  pairs.forEach(([oldKey, suffix]) => {
    const nextKey = storageKey(suffix);
    if (!localStorage.getItem(nextKey) && localStorage.getItem(oldKey)) {
      localStorage.setItem(nextKey, localStorage.getItem(oldKey));
    }
  });
}

function getCategoryLabelMap(ep) {
  const map = { ...LEGACY_CAT_NAMES };
  if (ep && Array.isArray(ep.vocabCategories)) {
    ep.vocabCategories.forEach(c => {
      if (c && c.key && c.label) map[c.key] = c.label;
    });
  }
  return map;
}

function renderVocabFilters(ep) {
  const group = document.getElementById("vocabFilterGroup");
  if (!group) return;

  const catMap = getCategoryLabelMap(ep);
  let cats = [];
  if (ep && Array.isArray(ep.vocabCategories) && ep.vocabCategories.length) {
    cats = ep.vocabCategories;
  } else {
    const seen = [];
    (VOCAB_LIST || []).forEach(v => {
      if (v.cat && !seen.includes(v.cat)) seen.push(v.cat);
    });
    cats = seen.map(k => ({ key: k, label: catMap[k] || k }));
  }

  group.innerHTML = [
    `<button class="btn-sm active" onclick="filterVocab('all', this)">全部 (${VOCAB_LIST.length})</button>`,
    ...cats.map(c => `<button class="btn-sm" onclick="filterVocab('${escapeAttr(c.key)}', this)">${escapeHtml(c.label)}</button>`)
  ].join("");
}

function renderWritingPrompt() {
  const ep = currentEpisode || {};
  const wp = ep.writingPrompt || (ep.id === "nvidia-scandal" ? DEFAULT_WRITING_PROMPT : null);

  const topicBox = document.getElementById("writingTopicBox");
  const skelBox = document.getElementById("writingSkeletonBox");
  const modelBox = document.getElementById("modelEssayBox");

  if (!wp) {
    if (topicBox) {
      topicBox.innerHTML = `<strong>复述任务：</strong>结合本视频核心观点与关键搭配，写一段 80–120 词英文总结或评论。`;
    }
    if (skelBox) {
      skelBox.innerHTML = `<div style="color:var(--text-muted);">建议尽量复用模块二中的核心词汇与模块三的句型结构。</div>`;
    }
    if (modelBox) {
      modelBox.innerHTML = `<strong>提示：</strong>本集未提供范文，可直接使用上方词汇和长难句模板进行仿写练习。`;
    }
    return;
  }

  if (topicBox) {
    topicBox.innerHTML = `<strong>辩题 / 写作主题：</strong>${escapeHtml(wp.topic || "")}`;
  }
  if (skelBox) {
    const proHtml = (wp.proPoints || []).map(p => `• ${p}<br/>`).join("");
    const conHtml = (wp.conPoints || []).map(p => `• ${p}<br/>`).join("");
    skelBox.innerHTML = `
      ${proHtml ? `<div><strong style="color:var(--accent);">视角一 / 正方骨架：</strong></div><div style="color:var(--text-muted); margin-bottom:8px;">${proHtml}</div>` : ""}
      ${conHtml ? `<div><strong style="color:var(--amber);">视角二 / 反方骨架：</strong></div><div style="color:var(--text-muted);">${conHtml}</div>` : ""}
    `;
  }
  if (modelBox) {
    modelBox.innerHTML = wp.modelEssay
      ? `<strong>参考范文：</strong><br/>${escapeHtml(wp.modelEssay)}`
      : `<strong>参考范文：</strong>暂无范文。`;
  }
}

function applyEpisode(episode) {
  currentEpisode = episode;
  CHAPTERS = episode.chapters || [];
  VOCAB_LIST = episode.vocab || [];
  SENTENCES = episode.sentences || [];
  CLOZE_ITEMS = episode.cloze || [];
  QUIZ_ITEMS = episode.quiz || [];

  migrateLegacyNvidiaStorage();
  masteredSet = new Set(JSON.parse(localStorage.getItem(storageKey("mastered")) || "[]"));
  notebookItems = loadNotebook();
  fcIndex = 0;
  fcFlipped = false;
  flashcardSource = "builtin";

  renderEpisodeMeta();
  renderChapters();
  renderVocabFilters(episode);
  renderVocab("all");
  renderSentences();
  renderFlashcard();
  renderCloze();
  renderQuiz();
  renderWritingPrompt();
  renderNotebook();

  const builtinBtn = document.getElementById("fcSourceBuiltin");
  const notebookBtn = document.getElementById("fcSourceNotebook");
  if (builtinBtn) {
    builtinBtn.classList.add("active");
    builtinBtn.textContent = `内置 ${VOCAB_LIST.length} 词`;
  }
  if (notebookBtn) notebookBtn.classList.remove("active");

  const essay = localStorage.getItem(storageKey("essay")) || "";
  const essayBox = document.getElementById("userEssayBox");
  if (essayBox) {
    essayBox.value = essay;
    saveEssayDraft();
  }
  const clozeScore = document.getElementById("clozeScore");
  const quizScore = document.getElementById("quizScoreText");
  if (clozeScore) clozeScore.textContent = "";
  if (quizScore) quizScore.textContent = "";
}

function renderEpisodeMeta() {
  const ep = currentEpisode;
  if (!ep) return;

  const ytPlayer = document.getElementById("ytPlayer");
  const localPlayer = document.getElementById("localPlayer");
  const ytLink = document.getElementById("ytSourceLink");

  if (ep.videoUrl && !ep.youtubeId) {
    if (ytPlayer) {
      ytPlayer.hidden = true;
      ytPlayer.src = "";
    }
    if (localPlayer) {
      localPlayer.hidden = false;
      if (localPlayer.getAttribute("data-src") !== ep.videoUrl) {
        localPlayer.src = ep.videoUrl;
        localPlayer.setAttribute("data-src", ep.videoUrl);
      }
    }
    if (ytLink) {
      ytLink.href = ep.videoUrl;
      ytLink.textContent = "打开原媒体文件 ↗";
    }
  } else {
    if (localPlayer) {
      localPlayer.pause();
      localPlayer.hidden = true;
      localPlayer.removeAttribute("src");
      localPlayer.removeAttribute("data-src");
    }
    if (ytPlayer) {
      ytPlayer.hidden = false;
      if (ep.youtubeId) {
        ytPlayer.src = `https://www.youtube.com/embed/${ep.youtubeId}?enablejsapi=1&rel=0`;
      } else {
        ytPlayer.src = "";
      }
    }
    if (ytLink) {
      ytLink.href = ep.sourceUrl || (ep.youtubeId ? `https://www.youtube.com/watch?v=${ep.youtubeId}` : "#");
      ytLink.textContent = "在 YouTube 打开原视频 ↗";
    }
  }

  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text || "";
  };
  setText("epShowTag", ep.show || "English Study Hub");
  setText("epDurationTag", ep.duration ? `时长 ${ep.duration}` : "");
  setText("epLevelTag", ep.level || "");
  setText("epAccentTag", ep.accent || "");
  setText("epTitle", ep.title || "");
  setText("epSummary", ep.summaryZh || "");
  document.title = `${ep.titleZh || ep.title || "Study Hub"} · Video English Study Hub`;

  const delBtn = document.getElementById("deleteEpisodeBtn");
  if (delBtn) {
    delBtn.hidden = !(ep.isCustom && ep.id !== "nvidia-scandal");
  }

  const speakerGrid = document.getElementById("speakerGrid");
  if (speakerGrid && Array.isArray(ep.speakers)) {
    speakerGrid.innerHTML = ep.speakers.map(s => `
      <div class="speaker-item">
        <strong>${escapeHtml(s.name || "")}</strong>
        ${escapeHtml(s.blurb || "")}
      </div>
    `).join("");
  }

  const navVocab = document.getElementById("navVocabBtn");
  if (navVocab) navVocab.textContent = `2. 核心词库 (${VOCAB_LIST.length})`;
  const navSent = document.getElementById("navSentencesBtn");
  if (navSent) navSent.textContent = `3. 长难句拆解 (${SENTENCES.length})`;
  updateNotebookNavCount();

  const vocabHeader = document.getElementById("vocabSectionTitle");
  if (vocabHeader) vocabHeader.textContent = `模块二：核心词汇与短语搭配库（${VOCAB_LIST.length} 组）`;
  const sentHeader = document.getElementById("sentencesSectionTitle");
  if (sentHeader) sentHeader.textContent = `模块三：核心原声金句与长难句语法拆解（${SENTENCES.length} 句）`;
}

function populateEpisodeSelect(list, selectedId) {
  currentEpisodeMetaList = list || [];
  const select = document.getElementById("episodeSelect");
  if (!select) return;
  select.innerHTML = list.map(item =>
    `<option value="${escapeAttr(item.id)}" ${item.id === selectedId ? "selected" : ""}>${item.isCustom ? "★ " : ""}${escapeHtml(item.titleZh || item.title || item.id)}</option>`
  ).join("");
}

async function switchEpisode(episodeId) {
  const index = await loadEpisodeIndex();
  const meta = index.find(e => e.id === episodeId) || index[0];
  if (!meta) throw new Error("No episodes registered in episodes/index.json");
  const payload = await loadEpisodePayload(meta);
  if (meta.isCustom !== undefined && payload.isCustom === undefined) {
    payload.isCustom = meta.isCustom;
  }
  applyEpisode(payload);
  populateEpisodeSelect(index, payload.id);
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("episode", payload.id);
    window.history.replaceState({}, "", url.toString());
  } catch (_) { /* ignore */ }
}

function seekVideo(seconds) {
  const sec = Math.max(0, Number(seconds) || 0);
  if (currentEpisode && currentEpisode.videoUrl && !currentEpisode.youtubeId) {
    const lp = document.getElementById("localPlayer");
    if (lp) {
      lp.currentTime = sec;
      lp.play().catch(() => {});
    }
  } else {
    const iframe = document.getElementById("ytPlayer");
    if (iframe && iframe.contentWindow) {
      iframe.contentWindow.postMessage(JSON.stringify({
        event: "command",
        func: "seekTo",
        args: [sec, true]
      }), "*");
      iframe.contentWindow.postMessage(JSON.stringify({
        event: "command",
        func: "playVideo",
        args: []
      }), "*");
    }
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function loadVoices() {
  if (!("speechSynthesis" in window)) return;
  cachedVoices = window.speechSynthesis.getVoices() || [];
  const select = document.getElementById("voiceSelect");
  if (!select || cachedVoices.length === 0) return;

  const currentVal = select.value || "auto-uk";
  const enVoices = cachedVoices.filter(v => /^en(-|_)/i.test(v.lang));
  if (enVoices.length === 0) return;

  enVoices.sort((a, b) => scoreVoice(b, "en-GB") - scoreVoice(a, "en-GB"));

  select.innerHTML = `
    <option value="auto-uk">高清英音 (UK Auto)</option>
    <option value="auto-us">高清美音 (US Auto)</option>
    ${enVoices.map(v => `<option value="${escapeAttr(v.name)}">${escapeHtml(v.name)} (${escapeHtml(v.lang)})</option>`).join("")}
  `;
  if ([...select.options].some(o => o.value === currentVal)) {
    select.value = currentVal;
  }
}

function scoreVoice(voice, targetLang = "en-GB") {
  if (!voice) return -999;
  const name = (voice.name || "").toLowerCase();
  const lang = (voice.lang || "").toLowerCase();
  let score = 0;

  if (!lang.startsWith("en")) return -1000;
  if (lang === targetLang.toLowerCase()) score += 40;
  else if (targetLang === "en-GB" && (lang.includes("gb") || lang.includes("uk"))) score += 35;
  else if (targetLang === "en-US" && lang.includes("us")) score += 35;

  if (name.includes("natural") || name.includes("neural")) score += 100;
  if (name.includes("online")) score += 60;
  if (name.includes("google uk english") || name.includes("google us english")) score += 85;
  if (name.includes("premium") || name.includes("enhanced") || name.includes("siri")) score += 75;
  if (/(sonia|ryan|libby|aria|jenny|guy|ava|andrew|emma|brian|daniel|serena|samantha|alex|allison)/i.test(name)) score += 45;
  if (voice.localService === false) score += 30;

  if (name.includes("compact") || name.includes("espeak") || name.includes("festival") || name.includes("mbrola")) {
    score -= 120;
  }
  return score;
}

function pickBestVoice(preferredLang = "en-GB") {
  if (!cachedVoices || cachedVoices.length === 0) {
    cachedVoices = ("speechSynthesis" in window) ? window.speechSynthesis.getVoices() : [];
  }
  const select = document.getElementById("voiceSelect");
  const selectedMode = select ? select.value : "auto-uk";

  if (selectedMode && selectedMode !== "auto-uk" && selectedMode !== "auto-us") {
    const manual = cachedVoices.find(v => v.name === selectedMode);
    if (manual) return manual;
  }

  const targetLang = selectedMode === "auto-us" ? "en-US" : (preferredLang || "en-GB");
  const candidates = cachedVoices.filter(v => /^en/i.test(v.lang));
  if (candidates.length === 0) return null;

  candidates.sort((a, b) => scoreVoice(b, targetLang) - scoreVoice(a, targetLang));
  return candidates[0];
}

function stopSpeaking() {
  if (speakTimer) {
    clearTimeout(speakTimer);
    speakTimer = null;
  }
  if (currentAudio) {
    currentAudio.pause();
    currentAudio.currentTime = 0;
    currentAudio = null;
  }
  if ("speechSynthesis" in window) {
    window.speechSynthesis.cancel();
  }
}

function onVoiceSelectChange() {
  stopSpeaking();
}

function cleanSpeechText(raw) {
  return String(raw || "")
    .replace(/\.{2,}/g, ", ")
    .replace(/—/g, ", ")
    .replace(/\s+/g, " ")
    .trim();
}

function speakText(text, lang = "en-GB", isShortTerm = false) {
  stopSpeaking();
  const cleaned = cleanSpeechText(text);
  if (!cleaned) return;

  const select = document.getElementById("voiceSelect");
  const selectedMode = select ? select.value : "auto-uk";
  const useUk = selectedMode === "auto-uk" || (selectedMode !== "auto-us" && lang === "en-GB");

  const wordCount = cleaned.split(/\s+/).length;
  if (isShortTerm || wordCount <= 4) {
    const dictType = useUk ? 1 : 2;
    const dictUrl = `https://dict.youdao.com/dictvoice?audio=${encodeURIComponent(cleaned)}&type=${dictType}`;
    const audio = new Audio(dictUrl);
    currentAudio = audio;
    let fallbackTriggered = false;
    const triggerFallback = () => {
      if (fallbackTriggered) return;
      fallbackTriggered = true;
      speakWithWebSpeech(cleaned, useUk ? "en-GB" : "en-US");
    };
    audio.onerror = triggerFallback;
    audio.play().catch(triggerFallback);
    return;
  }

  speakWithWebSpeech(cleaned, useUk ? "en-GB" : "en-US");
}

function speakWithWebSpeech(cleanedText, targetLang) {
  if (!("speechSynthesis" in window)) return;
  speakTimer = setTimeout(() => {
    const utter = new SpeechSynthesisUtterance(cleanedText);
    const bestVoice = pickBestVoice(targetLang);
    if (bestVoice) {
      utter.voice = bestVoice;
      utter.lang = bestVoice.lang;
    } else {
      utter.lang = targetLang;
    }
    utter.rate = 1.0;
    utter.pitch = 1.02;
    utter.volume = 1.0;
    window.speechSynthesis.speak(utter);
  }, 60);
}

if ("speechSynthesis" in window) {
  window.speechSynthesis.onvoiceschanged = loadVoices;
}

function renderChapters() {
  const container = document.getElementById("chapterContainer");
  if (!container) return;
  if (!CHAPTERS.length) {
    container.innerHTML = `<div class="card" style="color:var(--text-muted);">暂无章节内容。</div>`;
    return;
  }
  container.innerHTML = CHAPTERS.map(ch => {
    const turns = ch.turns || [{ sp: ch.speaker, en: ch.en, zh: ch.zh }];
    const fullChapterEn = turns.map(t => t.en).join(" ");
    return `
      <article class="chapter-card">
        <div class="chapter-top">
          <div class="chapter-title-wrap">
            <button class="time-btn" onclick="seekVideo(${ch.sec})" title="点击跳转到视频 ${escapeAttr(ch.time)}">▶ ${escapeHtml(ch.time)}</button>
            <strong style="font-size:16px;">Ch.${ch.num} ${escapeHtml(ch.title)}</strong>
            <span style="font-size:13px; color:var(--text-muted);">— ${escapeHtml(ch.titleZh || "")}</span>
          </div>
          <div style="display:flex; gap:8px; align-items:center;">
            <span class="tag accent">共 ${turns.length} 轮对话</span>
            <button class="btn-sm" onclick="speakText(${JSON.stringify(fullChapterEn).replace(/"/g, '&quot;')})">朗读全章</button>
          </div>
        </div>
        <div style="display:flex; flex-direction:column; gap:10px;">
          ${turns.map((t, idx) => {
            const isUkSpeaker = /\bUK\b|英音|Faisal/i.test(t.sp || "");
            const spLang = isUkSpeaker ? "en-GB" : "en-US";
            return `
              <div class="quote-box" style="margin-bottom:0;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                  <strong style="font-size:12px; color:${isUkSpeaker ? 'var(--amber)' : 'var(--accent)'};">
                    #${idx + 1} · ${escapeHtml(t.sp || "Speaker")}
                  </strong>
                  <button class="btn-sm" style="padding:2px 8px; font-size:11px;" onclick="speakText(${JSON.stringify(t.en || '').replace(/"/g, '&quot;')}, '${spLang}')">🔊 朗读此段</button>
                </div>
                <p class="en-text">${escapeHtml(t.en || "")}</p>
                <p class="zh-text">${escapeHtml(t.zh || "")}</p>
              </div>
            `;
          }).join("")}
        </div>
        <div class="takeaway-line" style="margin-top:12px;">
          <span>本章高频表达：</span>
          ${(ch.notes || []).map(n => `<span class="hl-term">${escapeHtml(n)}</span>`).join("")}
        </div>
      </article>
    `;
  }).join("");
}

function setLangMode(mode) {
  const list = document.getElementById("chapterContainer");
  if (!list) return;
  list.classList.remove("hide-zh", "hide-en");
  document.querySelectorAll("#sec-timeline .btn-sm").forEach(b => b.classList.remove("active"));
  if (mode === "en") {
    list.classList.add("hide-zh");
    document.getElementById("mode-en").classList.add("active");
  } else if (mode === "zh") {
    list.classList.add("hide-en");
    document.getElementById("mode-zh").classList.add("active");
  } else {
    document.getElementById("mode-both").classList.add("active");
  }
}

function renderVocab(cat = "all") {
  const container = document.getElementById("vocabContainer");
  if (!container) return;
  const list = cat === "all" ? VOCAB_LIST : VOCAB_LIST.filter(v => v.cat === cat);
  const catNames = getCategoryLabelMap(currentEpisode);
  container.innerHTML = list.map(v => `
    <div class="vocab-card">
      <div>
        <div class="vocab-head">
          <div>
            <div class="vocab-word">${escapeHtml(v.word)}</div>
            <div class="vocab-phonetic">${escapeHtml(v.phonetic || "")} ${v.pos ? "· " + escapeHtml(v.pos) : ""}</div>
          </div>
          <div style="display:flex; gap:6px; align-items:center;">
            <span class="tag accent">${escapeHtml(catNames[v.cat] || v.cat || "")}</span>
            <button class="btn-sm" onclick="speakText(${JSON.stringify(v.word).replace(/"/g, '&quot;')}, 'en-US', true)">发音</button>
          </div>
        </div>
        <div class="vocab-meaning" style="margin-top:8px;">${escapeHtml(v.zh || "")}</div>
      </div>
      <div class="vocab-example">
        <strong>原声语境：</strong>${escapeHtml(v.ex || "")}
      </div>
    </div>
  `).join("");
}

function filterVocab(cat, btn) {
  document.querySelectorAll("#vocabFilterGroup .btn-sm").forEach(b => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  renderVocab(cat);
}

function renderSentences() {
  const container = document.getElementById("sentenceContainer");
  if (!container) return;
  if (!SENTENCES.length) {
    container.innerHTML = `<div class="card" style="color:var(--text-muted);">暂无长难句条目。</div>`;
    return;
  }
  container.innerHTML = SENTENCES.map(s => `
    <div class="sentence-card">
      <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin-bottom:10px;">
        <div style="display:flex; align-items:center; gap:8px;">
          <button class="time-btn" onclick="seekVideo(${s.sec})">▶ ${escapeHtml(s.time)}</button>
          <span class="tag purple">${escapeHtml(s.pattern || "")}</span>
          ${s.speaker ? `<span class="tag">${escapeHtml(s.speaker)}</span>` : ""}
        </div>
        <button class="btn-sm" onclick="speakText(${JSON.stringify(s.en).replace(/"/g, '&quot;')})">朗读原句</button>
      </div>
      <div class="quote-box">
        <p class="en-text" style="font-weight:600;">${escapeHtml(s.en)}</p>
        <p class="zh-text">${escapeHtml(s.zh || "")}</p>
      </div>
      <div class="syntax-breakdown">
        ${(s.chunks || []).map(c => `<span class="syntax-chip">${escapeHtml(c)}</span>`).join("")}
      </div>
      ${s.imitation ? `<div style="font-size:13px; color:var(--green); margin-top:6px;">${escapeHtml(s.imitation)}</div>` : ""}
    </div>
  `).join("");
}

function getFlashcardDeck() {
  if (flashcardSource === "notebook") {
    return notebookItems.map(n => ({
      word: n.term,
      phonetic: n.phonetic || "",
      pos: "notebook",
      zh: n.zh || "（暂无释义，可在生词本中编辑）",
      ex: n.context || n.source || ""
    }));
  }
  return VOCAB_LIST;
}

function setFlashcardSource(source) {
  flashcardSource = source === "notebook" ? "notebook" : "builtin";
  const builtinBtn = document.getElementById("fcSourceBuiltin");
  const notebookBtn = document.getElementById("fcSourceNotebook");
  if (builtinBtn) {
    builtinBtn.classList.toggle("active", flashcardSource === "builtin");
    builtinBtn.textContent = `内置 ${VOCAB_LIST.length} 词`;
  }
  if (notebookBtn) notebookBtn.classList.toggle("active", flashcardSource === "notebook");
  fcIndex = 0;
  fcFlipped = false;
  renderFlashcard();
}

function renderFlashcard() {
  const deck = getFlashcardDeck();
  const stage = document.getElementById("flashcardStage");
  if (!stage) return;
  if (!deck.length) {
    stage.innerHTML = `<div style="color:var(--text-muted);">当前数据源为空。请先划词加入生词本，或切回内置词库。</div>`;
    const counter = document.getElementById("flashcardCounter");
    if (counter) counter.textContent = "0 / 0";
    return;
  }
  if (fcIndex >= deck.length) fcIndex = 0;
  const item = deck[fcIndex];
  const isMastered = masteredSet.has(item.word);
  if (!fcFlipped) {
    stage.innerHTML = `
      <div style="font-size:12px; color:var(--text-muted); margin-bottom:6px;">点击卡片翻转查看释义 (${isMastered ? "已掌握 ✓" : "待复习"})</div>
      <div style="font-size:24px; font-weight:700; color:var(--accent);">${escapeHtml(item.word)}</div>
      <div style="font-size:14px; color:var(--text-muted); margin-top:4px;">${escapeHtml(item.phonetic || "")} ${item.pos ? "· " + escapeHtml(item.pos) : ""}</div>
    `;
  } else {
    stage.innerHTML = `
      <div style="font-size:18px; font-weight:700; color:var(--green); margin-bottom:8px;">${escapeHtml(item.zh)}</div>
      <div style="font-size:13px; color:var(--text); max-width:520px;">${escapeHtml(item.ex)}</div>
    `;
  }
  const label = flashcardSource === "notebook" ? "生词本" : "内置词库";
  document.getElementById("flashcardCounter").textContent =
    `${label} · 第 ${fcIndex + 1} / ${deck.length} 张 · 已掌握 ${masteredSet.size} 个`;
}

function flipFlashcard() {
  if (!getFlashcardDeck().length) return;
  fcFlipped = !fcFlipped;
  renderFlashcard();
}

function nextFlashcard() {
  const deck = getFlashcardDeck();
  if (!deck.length) return;
  fcIndex = (fcIndex + 1) % deck.length;
  fcFlipped = false;
  renderFlashcard();
}

function prevFlashcard() {
  const deck = getFlashcardDeck();
  if (!deck.length) return;
  fcIndex = (fcIndex - 1 + deck.length) % deck.length;
  fcFlipped = false;
  renderFlashcard();
}

function markFlashcardMastered() {
  const deck = getFlashcardDeck();
  if (!deck.length) return;
  const w = deck[fcIndex].word;
  if (masteredSet.has(w)) masteredSet.delete(w);
  else masteredSet.add(w);
  localStorage.setItem(storageKey("mastered"), JSON.stringify([...masteredSet]));
  renderFlashcard();
}

function speakCurrentFlashcard() {
  const deck = getFlashcardDeck();
  if (!deck.length) return;
  speakText(deck[fcIndex].word, "en-US", true);
}

function loadNotebook() {
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey("notebook")) || "[]");
    return Array.isArray(raw) ? raw : [];
  } catch (_) {
    return [];
  }
}

function saveNotebook() {
  localStorage.setItem(storageKey("notebook"), JSON.stringify(notebookItems));
  updateNotebookNavCount();
}

function updateNotebookNavCount() {
  const btn = document.getElementById("navNotebookBtn");
  if (btn) btn.textContent = `5. 我的生词本 (${notebookItems.length})`;
}

function showToast(msg) {
  const el = document.getElementById("toastMsg");
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => { el.hidden = true; }, 2600);
}

function normalizeTerm(text) {
  return String(text || "")
    .replace(/[“”‘’]/g, '"')
    .replace(/^[^A-Za-z]+|[^A-Za-z]+$/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function isValidSelectionTerm(term) {
  if (!term) return false;
  if (!/^[A-Za-z][A-Za-z\s\-']{0,60}$/.test(term)) return false;
  const words = term.split(/\s+/).filter(Boolean);
  return words.length >= 1 && words.length <= 8;
}

function findBuiltinMatch(term) {
  const key = term.toLowerCase();
  return VOCAB_LIST.find(v => v.word.toLowerCase() === key) || null;
}

async function lookupDictionary(term) {
  try {
    const word = term.split(/\s+/)[0];
    const res = await fetch(`https://api.dictionaryapi.dev/api/v2/entries/en/${encodeURIComponent(word)}`);
    if (!res.ok) return { zh: "", phonetic: "" };
    const data = await res.json();
    const entry = data[0] || {};
    const phonetic = entry.phonetic || (entry.phonetics || []).map(p => p.text).find(Boolean) || "";
    const meaning = (((entry.meanings || [])[0] || {}).definitions || [])[0] || {};
    const enDef = meaning.definition || "";
    return { zh: enDef ? `英释义：${enDef}` : "", phonetic };
  } catch (_) {
    return { zh: "", phonetic: "" };
  }
}

function hideSelectionBubble() {
  const bubble = document.getElementById("selectionBubble");
  if (bubble) bubble.hidden = true;
  pendingSelection = null;
}

function showSelectionBubble(rect, payload) {
  const bubble = document.getElementById("selectionBubble");
  if (!bubble) return;
  pendingSelection = payload;
  bubble.hidden = false;
  const top = window.scrollY + rect.top - 44;
  const left = window.scrollX + rect.left + rect.width / 2 - 110;
  bubble.style.top = `${Math.max(8, top)}px`;
  bubble.style.left = `${Math.max(8, left)}px`;
}

function getSelectionContext(node) {
  if (!node) return { context: "", source: "" };
  const enEl = node.nodeType === 3 ? node.parentElement : node;
  const quote = enEl && enEl.closest ? enEl.closest(".quote-box, .vocab-example, .sentence-card, .chapter-card") : null;
  const enTextEl = quote ? quote.querySelector(".en-text") : (enEl && enEl.classList && enEl.classList.contains("en-text") ? enEl : null);
  const context = enTextEl ? enTextEl.textContent.trim() : (enEl ? enEl.textContent.trim() : "");
  const chapter = enEl && enEl.closest ? enEl.closest(".chapter-card") : null;
  let source = "";
  if (chapter) {
    const title = chapter.querySelector("strong");
    source = title ? title.textContent.trim() : "";
  }
  return { context: context.slice(0, 320), source };
}

function handleTextSelection() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) {
    hideSelectionBubble();
    return;
  }
  const term = normalizeTerm(sel.toString());
  if (!isValidSelectionTerm(term)) {
    hideSelectionBubble();
    return;
  }
  const range = sel.getRangeAt(0);
  const anchor = range.commonAncestorContainer;
  const el = anchor.nodeType === 3 ? anchor.parentElement : anchor;
  if (!el || !el.closest) {
    hideSelectionBubble();
    return;
  }
  if (el.closest("button, input, textarea, select, a, .selection-bubble, .nav-tabs, .toolbar-right, .modal-panel")) {
    hideSelectionBubble();
    return;
  }
  const readable = el.closest(".en-text, .vocab-example, .hl-term, .syntax-chip, .chapter-card, .sentence-card, .vocab-card");
  if (!readable) {
    hideSelectionBubble();
    return;
  }
  const { context, source } = getSelectionContext(anchor);
  const rect = range.getBoundingClientRect();
  if (!rect.width && !rect.height) {
    hideSelectionBubble();
    return;
  }
  showSelectionBubble(rect, { term, context, source });
}

async function addPendingToNotebook() {
  if (!pendingSelection || !pendingSelection.term) return;
  const term = pendingSelection.term;
  const exists = notebookItems.find(n => n.term.toLowerCase() === term.toLowerCase());
  if (exists) {
    showToast(`「${term}」已在生词本中`);
    hideSelectionBubble();
    scrollToSection("sec-notebook");
    renderNotebook();
    return;
  }

  const builtin = findBuiltinMatch(term);
  let zh = builtin ? builtin.zh : "";
  let phonetic = builtin ? builtin.phonetic : "";
  if (!zh) {
    showToast("正在查询释义…");
    const looked = await lookupDictionary(term);
    zh = looked.zh;
    phonetic = looked.phonetic || phonetic;
  }

  const item = {
    id: `nb_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
    term,
    zh: zh || "",
    phonetic: phonetic || "",
    context: pendingSelection.context || "",
    source: pendingSelection.source || "",
    createdAt: Date.now()
  };
  notebookItems.unshift(item);
  saveNotebook();
  renderNotebook();
  hideSelectionBubble();
  showToast(`已加入生词本：${term}`);
  if (window.getSelection) window.getSelection().removeAllRanges();
}

function renderNotebook() {
  const container = document.getElementById("notebookContainer");
  const empty = document.getElementById("notebookEmpty");
  if (!container) return;
  updateNotebookNavCount();
  if (!notebookItems.length) {
    container.innerHTML = "";
    if (empty) empty.style.display = "block";
    return;
  }
  if (empty) empty.style.display = "none";
  container.innerHTML = notebookItems.map(n => `
    <div class="notebook-card" id="nbcard_${n.id}">
      <div class="nb-term">${escapeHtml(n.term)}</div>
      <div class="nb-meta">${escapeHtml(n.phonetic || "")} ${n.source ? "· " + escapeHtml(n.source) : ""} · ${new Date(n.createdAt).toLocaleString()}</div>
      <input class="nb-zh-input" value="${escapeAttr(n.zh || "")}" placeholder="填写或修改中文释义 / 笔记" onchange="updateNotebookZh('${n.id}', this.value)" />
      ${n.context ? `<div class="nb-context"><strong>原文语境：</strong>${escapeHtml(n.context)}</div>` : ""}
      <div class="nb-actions">
        <button class="btn-sm" onclick="speakText(${JSON.stringify(n.term).replace(/"/g, '&quot;')}, 'en-US', true)">发音</button>
        <button class="btn-sm" style="color:var(--rose);" onclick="removeNotebookItem('${n.id}')">删除</button>
      </div>
    </div>
  `).join("");
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, "&#39;");
}

function updateNotebookZh(id, value) {
  const item = notebookItems.find(n => n.id === id);
  if (!item) return;
  item.zh = value;
  saveNotebook();
  if (flashcardSource === "notebook") renderFlashcard();
}

function removeNotebookItem(id) {
  notebookItems = notebookItems.filter(n => n.id !== id);
  saveNotebook();
  renderNotebook();
  if (flashcardSource === "notebook") {
    fcIndex = 0;
    renderFlashcard();
  }
  showToast("已从生词本删除");
}

function clearNotebook() {
  if (!notebookItems.length) {
    showToast("生词本已经是空的");
    return;
  }
  if (!confirm("确定清空全部生词本？此操作不可撤销。")) return;
  notebookItems = [];
  saveNotebook();
  renderNotebook();
  if (flashcardSource === "notebook") {
    fcIndex = 0;
    renderFlashcard();
  }
  showToast("生词本已清空");
}

function exportNotebookCsv() {
  if (!notebookItems.length) {
    showToast("生词本为空，无法导出");
    return;
  }
  const header = "Term,Phonetic,ChineseMeaning,Context,Source,CreatedAt\n";
  const rows = notebookItems.map(n => {
    const esc = v => `"${String(v || "").replace(/"/g, '""')}"`;
    return [esc(n.term), esc(n.phonetic), esc(n.zh), esc(n.context), esc(n.source), esc(new Date(n.createdAt).toISOString())].join(",");
  }).join("\n");
  const blob = new Blob([header + rows], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `my-notebook-${(currentEpisode && currentEpisode.id) || "episode"}.csv`;
  a.click();
  URL.revokeObjectURL(url);
  showToast("已导出生词本 CSV");
}

function exportCurrentEpisodeJson() {
  if (!currentEpisode) return;
  const blob = new Blob([JSON.stringify(currentEpisode, null, 2)], { type: "application/json;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${currentEpisode.id || "episode"}.json`;
  a.click();
  URL.revokeObjectURL(url);
  showToast(`已导出 ${currentEpisode.id}.json`);
}

function initSelectionHandlers() {
  document.addEventListener("mouseup", (e) => {
    if (e.target && e.target.closest && e.target.closest(".selection-bubble")) return;
    setTimeout(() => handleTextSelection(), 10);
  });
  document.addEventListener("touchend", (e) => {
    if (e.target && e.target.closest && e.target.closest(".selection-bubble")) return;
    setTimeout(() => handleTextSelection(), 50);
  });
  document.addEventListener("scroll", hideSelectionBubble, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      hideSelectionBubble();
      closeImportModal();
    }
  });

  const addBtn = document.getElementById("bubbleAddBtn");
  const speakBtn = document.getElementById("bubbleSpeakBtn");
  const closeBtn = document.getElementById("bubbleCloseBtn");
  if (addBtn) addBtn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); addPendingToNotebook(); });
  if (speakBtn) speakBtn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (pendingSelection) speakText(pendingSelection.term, "en-US", true);
  });
  if (closeBtn) closeBtn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); hideSelectionBubble(); });
}

function renderCloze() {
  const container = document.getElementById("clozeContainer");
  if (!container) return;
  if (!CLOZE_ITEMS.length) {
    container.innerHTML = `<div style="color:var(--text-muted);">暂无完形填空题目。</div>`;
    return;
  }
  container.innerHTML = CLOZE_ITEMS.map((c, idx) => `
    <div style="background:var(--bg-elevated); padding:10px 12px; border-radius:8px;">
      <span style="color:var(--text-muted); font-size:12px;">[${escapeHtml(c.time || "")}] #${idx + 1}: </span>
      <span>${escapeHtml(c.prefix || "")}</span>
      <input type="text" class="cloze-input" id="cloze_${escapeAttr(c.id)}" placeholder="${escapeAttr(c.hint || "")}" />
      <span>${escapeHtml(c.suffix || "")}</span>
    </div>
  `).join("");
}

function checkClozeAnswers() {
  let right = 0;
  CLOZE_ITEMS.forEach(c => {
    const input = document.getElementById("cloze_" + c.id);
    if (!input) return;
    const val = (input.value || "").trim().toLowerCase();
    input.classList.remove("correct", "wrong");
    if (val === String(c.answer || "").toLowerCase()) {
      input.classList.add("correct");
      right++;
    } else {
      input.classList.add("wrong");
    }
  });
  document.getElementById("clozeScore").textContent = `得分：${right} / ${CLOZE_ITEMS.length}`;
}

function revealClozeAnswers() {
  CLOZE_ITEMS.forEach(c => {
    const input = document.getElementById("cloze_" + c.id);
    if (!input) return;
    input.value = c.answer;
    input.classList.remove("wrong");
    input.classList.add("correct");
  });
  document.getElementById("clozeScore").textContent = `已显示全部标准答案`;
}

function renderQuiz() {
  const container = document.getElementById("quizContainer");
  if (!container) return;
  if (!QUIZ_ITEMS.length) {
    container.innerHTML = `<div style="color:var(--text-muted);">暂无理解测验题目。</div>`;
    return;
  }
  container.innerHTML = QUIZ_ITEMS.map((item, qIdx) => `
    <div class="quiz-item">
      <div style="font-weight:600; font-size:14px; margin-bottom:6px;">${escapeHtml(item.q)}</div>
      ${(item.options || []).map((opt, oIdx) => `
        <label class="quiz-option">
          <input type="radio" name="quiz_${qIdx}" value="${oIdx}" style="margin-right:6px;" />
          ${escapeHtml(opt)}
        </label>
      `).join("")}
      <div id="quiz_exp_${qIdx}" style="display:none; margin-top:8px; font-size:12px; padding:8px; border-radius:6px; background:var(--bg-card);"></div>
    </div>
  `).join("");
}

function submitQuiz() {
  let score = 0;
  QUIZ_ITEMS.forEach((item, qIdx) => {
    const selected = document.querySelector(`input[name="quiz_${qIdx}"]:checked`);
    const expBox = document.getElementById(`quiz_exp_${qIdx}`);
    if (!expBox) return;
    expBox.style.display = "block";
    const isRight = selected && Number(selected.value) === item.correct;
    if (isRight) score++;
    expBox.style.borderLeft = isRight ? "3px solid var(--green)" : "3px solid var(--rose)";
    expBox.innerHTML = `<strong style="color:${isRight ? 'var(--green)' : 'var(--rose)'};">${isRight ? "回答正确 ✓" : "回答有误 ✗"}</strong> · ${escapeHtml(item.explanation || "")}`;
  });
  document.getElementById("quizScoreText").textContent = `测验得分：${score} / ${QUIZ_ITEMS.length}`;
}

function saveEssayDraft() {
  const box = document.getElementById("userEssayBox");
  if (!box) return;
  localStorage.setItem(storageKey("essay"), box.value);
  const words = box.value.trim() ? box.value.trim().split(/\s+/).length : 0;
  const counter = document.getElementById("essayWordCount");
  if (counter) counter.textContent = `${words} words`;
}

function toggleModelEssay() {
  const box = document.getElementById("modelEssayBox");
  if (!box) return;
  box.style.display = box.style.display === "none" ? "block" : "none";
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme");
  if (current === "light") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", "light");
  }
}

function scrollToSection(id) {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

function resetAllPractice() {
  localStorage.removeItem(storageKey("mastered"));
  localStorage.removeItem(storageKey("essay"));
  masteredSet.clear();
  const essayBox = document.getElementById("userEssayBox");
  if (essayBox) essayBox.value = "";
  saveEssayDraft();
  renderFlashcard();
  renderCloze();
  renderQuiz();
  const clozeScore = document.getElementById("clozeScore");
  const quizScore = document.getElementById("quizScoreText");
  if (clozeScore) clozeScore.textContent = "";
  if (quizScore) quizScore.textContent = "";
  showToast("已重置当前剧集的练习记录（生词本未删除）");
}

/* ======================== 导入任意视频交互 ======================== */

function loadSavedApiConfig() {
  try {
    return JSON.parse(localStorage.getItem("studyhub:api_config") || "{}") || {};
  } catch (_) {
    return {};
  }
}

function saveApiConfigFromInputs() {
  const cfg = {
    apiKey: (document.getElementById("cfgApiKey")?.value || "").trim(),
    baseUrl: (document.getElementById("cfgBaseUrl")?.value || "").trim(),
    llmModel: (document.getElementById("cfgLlmModel")?.value || "").trim(),
    asrModel: (document.getElementById("cfgAsrModel")?.value || "").trim()
  };
  localStorage.setItem("studyhub:api_config", JSON.stringify(cfg));
  return cfg;
}

function applySavedApiConfigToInputs() {
  const cfg = loadSavedApiConfig();
  if (document.getElementById("cfgApiKey")) document.getElementById("cfgApiKey").value = cfg.apiKey || "";
  if (document.getElementById("cfgBaseUrl")) document.getElementById("cfgBaseUrl").value = cfg.baseUrl || "";
  if (document.getElementById("cfgLlmModel")) document.getElementById("cfgLlmModel").value = cfg.llmModel || "";
  if (document.getElementById("cfgAsrModel")) document.getElementById("cfgAsrModel").value = cfg.asrModel || "";
}

async function checkBackendHealth() {
  const banner = document.getElementById("backendHealthBanner");
  const details = document.getElementById("apiConfigDetails");
  if (!banner) return;
  try {
    const res = await fetch("/api/health");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (data.hasLlmKey) {
      banner.className = "health-banner ok";
      banner.textContent = `后端服务在线：已配置默认模型 (${data.llmModel} / ${data.asrModel})，可直接丢入视频生成！`;
    } else {
      const saved = loadSavedApiConfig();
      banner.className = "health-banner warn";
      banner.textContent = saved.apiKey
        ? "后端服务在线：将使用你在本机保存的 API Key 进行生成。"
        : "后端服务在线，但未检测到服务端 .env 密钥：请在下方展开「API 与模型设置」填入 Key，或勾选「演示模式」。";
      if (!saved.apiKey && details) details.open = true;
    }
  } catch (_) {
    banner.className = "health-banner warn";
    banner.textContent = "未检测到后端服务（/api/health 不可达）。请通过 uvicorn server.app:app 启动服务。";
  }
}

function openImportModal() {
  const modal = document.getElementById("importModal");
  if (!modal) return;
  modal.hidden = false;
  applySavedApiConfigToInputs();
  const progressBox = document.getElementById("importProgressBox");
  if (progressBox && !activeJobTimer) progressBox.hidden = true;
  checkBackendHealth();
}

function closeImportModal() {
  const modal = document.getElementById("importModal");
  if (modal) modal.hidden = true;
}

function switchImportTab(mode) {
  importMode = mode === "file" ? "file" : "url";
  const urlBtn = document.getElementById("tabUrlBtn");
  const fileBtn = document.getElementById("tabFileBtn");
  const urlTab = document.getElementById("importTabUrl");
  const fileTab = document.getElementById("importTabFile");
  if (urlBtn) urlBtn.classList.toggle("active", importMode === "url");
  if (fileBtn) fileBtn.classList.toggle("active", importMode === "file");
  if (urlTab) urlTab.hidden = importMode !== "url";
  if (fileTab) fileTab.hidden = importMode !== "file";
}

function setSelectedFile(file) {
  selectedUploadFile = file || null;
  const textEl = document.getElementById("fileDropText");
  if (!textEl) return;
  if (file) {
    const mb = (file.size / (1024 * 1024)).toFixed(2);
    textEl.innerHTML = `已选择：<strong style="color:var(--accent);">${escapeHtml(file.name)}</strong> (${mb} MB)`;
  } else {
    textEl.innerHTML = `点击选择文件，或将 <code>.mp4 / .mov / .webm / .mp3 / .m4a</code> 拖到此处`;
  }
}

function initImportHandlers() {
  const dropZone = document.getElementById("fileDropZone");
  const fileInput = document.getElementById("importFileInput");
  if (dropZone && fileInput) {
    dropZone.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      if (fileInput.files && fileInput.files[0]) setSelectedFile(fileInput.files[0]);
    });
    dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropZone.classList.remove("dragover");
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
        setSelectedFile(e.dataTransfer.files[0]);
      }
    });
  }
}

function updateProgressUI(pct, text, logs) {
  const box = document.getElementById("importProgressBox");
  const pctEl = document.getElementById("importProgressPct");
  const txtEl = document.getElementById("importProgressText");
  const fillEl = document.getElementById("importProgressFill");
  const logsEl = document.getElementById("importProgressLogs");
  if (box) box.hidden = false;
  if (pctEl) pctEl.textContent = `${pct}%`;
  if (txtEl) txtEl.textContent = text || "";
  if (fillEl) fillEl.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  if (logsEl && Array.isArray(logs)) {
    logsEl.innerHTML = logs.map(l => `<div>• ${escapeHtml(l)}</div>`).join("");
    logsEl.scrollTop = logsEl.scrollHeight;
  }
}

async function submitVideoImport() {
  const submitBtn = document.getElementById("submitImportBtn");
  const title = (document.getElementById("importTitleInput")?.value || "").trim();
  const level = (document.getElementById("importLevelSelect")?.value || "").trim();
  const mockMode = Boolean(document.getElementById("importMockCheckbox")?.checked);
  const cfg = saveApiConfigFromInputs();
  const apiConfig = (cfg.apiKey || cfg.baseUrl || cfg.llmModel || cfg.asrModel) ? {
    apiKey: cfg.apiKey || null,
    baseUrl: cfg.baseUrl || null,
    llmModel: cfg.llmModel || null,
    asrModel: cfg.asrModel || null
  } : null;

  try {
    if (submitBtn) submitBtn.disabled = true;
    let jobRes;

    if (importMode === "url") {
      const url = (document.getElementById("importUrlInput")?.value || "").trim();
      if (!url) {
        showToast("请先输入 YouTube 视频链接");
        if (submitBtn) submitBtn.disabled = false;
        return;
      }
      updateProgressUI(5, "正在提交 YouTube 链接…", ["正在连接后端接口..."]);
      jobRes = await fetch("/api/episodes/import-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, title: title || null, level: level || null, apiConfig, mockMode })
      });
    } else {
      if (!selectedUploadFile) {
        showToast("请先选择或拖入本地视频/音频文件");
        if (submitBtn) submitBtn.disabled = false;
        return;
      }
      updateProgressUI(5, "正在上传本地媒体文件…", [`上传文件：${selectedUploadFile.name}`]);
      const fd = new FormData();
      fd.append("file", selectedUploadFile);
      if (title) fd.append("title", title);
      if (level) fd.append("level", level);
      if (apiConfig) fd.append("apiConfigJson", JSON.stringify(apiConfig));
      fd.append("mockMode", mockMode ? "true" : "false");
      jobRes = await fetch("/api/episodes/import-file", { method: "POST", body: fd });
    }

    if (!jobRes.ok) {
      let errMsg = `HTTP ${jobRes.status}`;
      try {
        const errData = await jobRes.json();
        errMsg = errData.detail || errMsg;
      } catch (_) { /* ignore */ }
      throw new Error(errMsg);
    }

    const { jobId } = await jobRes.json();
    pollImportJob(jobId);
  } catch (err) {
    updateProgressUI(100, `导入失败：${err.message}`, [`错误：${err.message}`]);
    showToast(`导入失败：${err.message}`);
    if (submitBtn) submitBtn.disabled = false;
  }
}

function pollImportJob(jobId) {
  if (activeJobTimer) clearInterval(activeJobTimer);
  const submitBtn = document.getElementById("submitImportBtn");

  const tick = async () => {
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const job = await res.json();
      updateProgressUI(job.progress || 0, job.message || "", job.logs || []);

      if (job.status === "done" && job.episodeId) {
        clearInterval(activeJobTimer);
        activeJobTimer = null;
        if (submitBtn) submitBtn.disabled = false;
        await switchEpisode(job.episodeId);
        closeImportModal();
        window.scrollTo({ top: 0, behavior: "smooth" });
        showToast("整站学习内容已根据新视频完成重构！");
      } else if (job.status === "error") {
        clearInterval(activeJobTimer);
        activeJobTimer = null;
        if (submitBtn) submitBtn.disabled = false;
        showToast(`生成失败：${job.error || job.message}`);
      }
    } catch (err) {
      clearInterval(activeJobTimer);
      activeJobTimer = null;
      if (submitBtn) submitBtn.disabled = false;
      updateProgressUI(100, `轮询失败：${err.message}`, [`轮询异常：${err.message}`]);
    }
  };

  tick();
  activeJobTimer = setInterval(tick, 1200);
}

async function deleteCurrentEpisode() {
  if (!currentEpisode || !currentEpisode.isCustom || currentEpisode.id === "nvidia-scandal") return;
  if (!confirm(`确定删除自定义剧集「${currentEpisode.titleZh || currentEpisode.title}」吗？`)) return;
  try {
    const res = await fetch(`/api/episodes/${encodeURIComponent(currentEpisode.id)}`, { method: "DELETE" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `HTTP ${res.status}`);
    }
    showToast("已删除该自定义剧集");
    await switchEpisode("");
  } catch (err) {
    showToast(`删除失败：${err.message}`);
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  loadVoices();
  initSelectionHandlers();
  initImportHandlers();

  const select = document.getElementById("episodeSelect");
  if (select) {
    select.addEventListener("change", () => {
      switchEpisode(select.value).catch(err => {
        console.error(err);
        showToast("切换剧集失败：" + err.message);
      });
    });
  }

  try {
    const index = await loadEpisodeIndex();
    const preferred = getQueryEpisodeId() || (index[0] && index[0].id);
    await switchEpisode(preferred);
  } catch (err) {
    console.error(err);
    const container = document.getElementById("chapterContainer");
    if (container) {
      container.innerHTML = `<div class="card" style="color:var(--rose);">剧集加载失败：${escapeHtml(err.message)}。请通过 uvicorn server.app:app 启动服务并确认存在 episodes/index.json。</div>`;
    }
  }
});
