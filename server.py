#!/usr/bin/env python3
"""小宇宙专题研究助手：本地网页入口与 Codex 任务调度。"""

from __future__ import annotations

import json
import importlib.util
import base64
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from html import escape
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from tencent_asr import (
    credentials_status as tencent_credentials_status,
    save_credentials as save_tencent_credentials,
    test_credentials as test_tencent_credentials,
)

try:
    from groq_asr import (
        credentials_status as groq_credentials_status,
        save_api_key as save_groq_api_key,
        test_credentials as test_groq_credentials,
    )
except Exception:  # noqa: BLE001
    groq_credentials_status = None
    save_groq_api_key = None
    test_groq_credentials = None

APP_DIR = Path(__file__).resolve().parent
HOST = os.environ.get("PODCAST_ASSISTANT_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", os.environ.get("PODCAST_ASSISTANT_PORT", "8765")))
ROOT = Path(os.environ.get("PODCAST_ASSISTANT_ROOT", str(APP_DIR.parent))).expanduser().resolve()
LIBRARY = Path(os.environ.get("PODCAST_LIBRARY_DIR", str(ROOT / "网页书架"))).expanduser().resolve()
JOBS_DIR = Path(os.environ.get("PODCAST_JOBS_DIR", str(APP_DIR / "jobs"))).expanduser().resolve()
CODEX = Path(os.environ.get("CODEX_BIN", "/Applications/Codex.app/Contents/Resources/codex")).expanduser()
SKILL_DIR = Path(os.environ.get("CODEX_SKILL_DIR", "/Users/biu/.codex/skills")).expanduser()
VENDORED_PIPELINE_DIR = APP_DIR / "vendor" / "xiaoyuzhou-illustrated-ebook" / "scripts"
PIPELINE_DIR = Path(
    os.environ.get(
        "PODCAST_PIPELINE_DIR",
        str(VENDORED_PIPELINE_DIR if VENDORED_PIPELINE_DIR.exists() else SKILL_DIR / "xiaoyuzhou-illustrated-ebook" / "scripts"),
    )
).expanduser().resolve()
PIPELINE_SCRIPT = PIPELINE_DIR / "pipeline.py"
TRANSCRIPT_CACHE = Path(
    os.environ.get(
        "PODCAST_TRANSCRIPT_CACHE",
        "~/Library/Caches/xiaoyuzhou-illustrated-ebook/transcript-cache",
    )
).expanduser()
MAX_INPUT = 180_000
QUICK_OUTPUT_POLL_SECONDS = 2
QUICK_ENGINE = os.environ.get("PODCAST_QUICK_ENGINE", "agent").strip().lower()
COMPARISON_ENGINE = os.environ.get("PODCAST_COMPARISON_ENGINE", "codex").strip().lower()
AUTH_USER = os.environ.get("PODCAST_AUTH_USER", "podcast")
AUTH_PASSWORD = os.environ.get("PODCAST_AUTH_PASSWORD", "")
DEMO_MODE = os.environ.get("PODCAST_DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
if DEMO_MODE and "PODCAST_QUICK_ENGINE" not in os.environ:
    QUICK_ENGINE = "pipeline"
if DEMO_MODE and "PODCAST_COMPARISON_ENGINE" not in os.environ:
    COMPARISON_ENGINE = "deterministic"

JOBS_DIR.mkdir(parents=True, exist_ok=True)
CLIPBOARD_SESSIONS: dict[str, dict] = {}
CLIPBOARD_LOCK = threading.Lock()


def asr_status() -> dict:
    groq = (
        groq_credentials_status()
        if groq_credentials_status
        else {"configured": False, "error": "Groq ASR 模块不可用"}
    )
    tencent = tencent_credentials_status()
    preferred = "groq" if groq.get("configured") else ("tencent" if tencent.get("configured") else "")
    return {
        "configured": bool(preferred),
        "preferred": preferred,
        "groq": groq,
        "tencent": tencent,
    }

CROSS_EPISODE_COMPARISON = """
跨集比对要求（本产品的招牌模块）：
- 不要只做“逐集摘要再综上所述”。必须先拆观点，再比关系。
- 在项目根目录额外生成 `<项目>/观点原子.md`，作为中间产物，不进入 HTML 正文。
  建议用表格列出：claim_id｜单集/来源｜说话人｜原子观点｜支撑原话或依据｜source｜confidence｜topic_hint。
- 在项目根目录生成 `<项目>/跨集比对.md`，它会被 build-html 自动放到正文最前面。
- `跨集比对.md` 必须包含：
  1. 依据构成：🎧 听稿依据/文稿几期，📝 简介依据几期，🌐 联网核验补充，🤔 AI 推断多少；
  2. 观点矩阵：子议题 × 单集/嘉宾 × 关系；
  3. 大家争的是什么：优先写分歧、部分分歧和信息冲突；
  4. 没争议的共识；
  5. 只有某一期提到的独到观点；
  6. 需要存疑的地方；
  7. 各期速览。
- 观点矩阵每个格子都必须带来源徽标：
  `<span class="source-badge source-audio">🎧 听稿依据</span>`
  `<span class="source-badge source-intro">📝 简介依据</span>`
  `<span class="source-badge source-web">🌐 联网核验</span>`
  `<span class="source-badge source-infer">🤔 AI 推断</span>`
- 关系用：✅共识 / ⚠️分歧 / ◐部分分歧 / 💡独有 / ❓信息冲突 / —未提及。
- 空格子必须留空或写 `—未提及`，不得为了填满矩阵而编。
- 只有听稿依据、官方文稿或用户粘贴文稿可以写支撑原话；简介依据和 AI 推断不能写成主播原话。
- 如果只有 1 期节目，仍生成 `观点原子.md` 和 `跨集比对.md`，但标题改为“单集观点结构”，不得伪造跨集分歧。
"""


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_clipboard() -> str:
    if sys.platform != "darwin":
        return ""
    try:
        result = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout or ""
    except Exception:
        return ""


def looks_like_transcript(value: str) -> bool:
    text = value.strip()
    if len(text) < 400 or len(text) > MAX_INPUT:
        return False
    cjk = sum("\u4e00" <= char <= "\u9fff" for char in text)
    if cjk / max(len(text), 1) < 0.25:
        return False
    if sum(text.count(char) for char in "。，！？；：,.!?;:") < 8:
        return False
    return True


def clipboard_status(session_id: str) -> dict | None:
    with CLIPBOARD_LOCK:
        session = CLIPBOARD_SESSIONS.get(session_id)
        return dict(session) if session else None


def update_clipboard_status(session_id: str, **updates) -> None:
    with CLIPBOARD_LOCK:
        session = CLIPBOARD_SESSIONS.get(session_id)
        if not session:
            return
        session.update(updates)
        session["updated_at"] = now()


def watch_clipboard(session_id: str, timeout_seconds: int = 120) -> None:
    baseline = read_clipboard()
    start = time.time()
    while time.time() - start < timeout_seconds:
        current = read_clipboard()
        if current and current != baseline and looks_like_transcript(current):
            cache_user_paste_if_possible(current)
            update_clipboard_status(
                session_id,
                status="captured",
                text=current,
                captured_at=now(),
                message="已捕获小宇宙文稿。",
            )
            return
        time.sleep(0.8)
    update_clipboard_status(
        session_id,
        status="timeout",
        message="没有在 120 秒内检测到新的长文稿，可以手动粘贴。",
    )


def job_paths(job_id: str) -> tuple[Path, Path]:
    return JOBS_DIR / f"{job_id}.json", JOBS_DIR / f"{job_id}.log"


def save_job(job: dict) -> None:
    state_path, _ = job_paths(job["id"])
    temp_path = state_path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp_path.replace(state_path)


def load_job(job_id: str) -> dict | None:
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        return None
    state_path, _ = job_paths(job_id)
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def list_jobs() -> list[dict]:
    jobs = []
    for path in JOBS_DIR.glob("*.json"):
        try:
            jobs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(jobs, key=lambda item: item.get("created_at", ""), reverse=True)[:20]


def library_items() -> list[dict]:
    items = []
    if not LIBRARY.exists():
        return items
    for path in LIBRARY.glob("*.html"):
        if path.name == "index.html":
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
        except OSError:
            mtime = ""
        items.append(
            {
                "name": path.name,
                "title": path.stem,
                "date": mtime,
                "url": f"/library/{quote(path.name)}",
            }
        )
    return sorted(items, key=lambda item: item["date"], reverse=True)


def render_library_index() -> bytes:
    cards = "\n".join(
        f"""<a class="book-card" href="{item['url']}">
          <span class="book-title">{escape(item['title'])}</span>
          <time>{escape(item['date'])}</time>
        </a>"""
        for item in library_items()
    )
    if not cards:
        cards = """<div class="empty">还没有生成过专题。回到研究所，先做第一份。</div>"""
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>网页书架 · 播客研究所</title>
  <style>
    * {{ box-sizing:border-box }}
    body {{
      margin:0;
      min-height:100vh;
      color:#f0eadc;
      background:radial-gradient(circle at 50% -10%, #343540 0, #171821 36%, #090a0e 72%, #050506 100%);
      font-family:"PingFang SC","Noto Sans SC",-apple-system,BlinkMacSystemFont,sans-serif;
    }}
    main {{ width:min(920px, calc(100% - 28px)); margin:0 auto; padding:30px 0 78px }}
    .topbar {{ display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:22px }}
    .brand {{ display:flex; align-items:center; gap:12px; font-weight:900; letter-spacing:.08em }}
    .brand-mark {{ display:grid; place-items:center; width:22px; height:22px; border-radius:2px; color:#13131c; background:#ff5c1a; font:900 .62rem "Courier New",monospace; box-shadow:0 0 10px #ff5c1a80,0 9px 22px #0008 }}
    .home-link {{ border:1px solid #434456; border-radius:2px; background:#15161d; color:#d7d3cb; text-decoration:none; padding:8px 13px; font:900 .72rem "Courier New",monospace; letter-spacing:.06em; box-shadow:inset 0 1px 0 #ffffff12,0 8px 16px #0007 }}
    .home-link:hover {{ border-color:#ff5c1a; color:#ff8b57 }}
    .hero {{ border:1px solid #3a3a4e; background:linear-gradient(180deg,#242531,#15161d); box-shadow:0 30px 80px #000b,inset 0 1px 0 #ffffff12; padding:22px; margin-bottom:20px }}
    .screen {{ border:2px solid #0c1626; background:#040e1c; min-height:188px; padding:26px 24px; box-shadow:inset 0 0 50px #000d,0 0 0 7px #101119; position:relative; overflow:hidden }}
    .screen::before {{ content:""; position:absolute; inset:0; background:repeating-linear-gradient(0deg,transparent 0 2px,#0003 2px 3px),radial-gradient(ellipse at center,transparent 35%,#0009 100%); pointer-events:none; z-index:3 }}
    .eyebrow {{ position:relative; z-index:4; color:#ff5c1a; font:900 .62rem "Courier New",monospace; letter-spacing:.28em; margin-bottom:4px; opacity:.55 }}
    h1 {{ position:relative; z-index:4; margin:0; color:#ff9060; font-family:"Songti SC","Noto Serif SC",serif; font-size:24px; line-height:1.2; letter-spacing:.04em; text-shadow:0 0 14px #ff5c1aa6 }}
    h1 span {{ animation:cursorBlink 1.1s linear infinite; color:#ff5c1a; font-size:18px; margin-left:4px }}
    p {{ position:relative; z-index:4; color:#ff5c1a; margin:5px 0 0; font:900 .62rem "Courier New",monospace; letter-spacing:.12em; opacity:.38 }}
    .screen-bars {{ align-items:end; display:flex; gap:2px; height:44px; margin-top:25px; position:relative; z-index:4 }}
    .screen-bars i {{ animation:barPulse 780ms ease-in-out infinite alternate; background:#ff5c1a; box-shadow:0 0 9px #ff5c1a66; min-height:5px; width:3px }}
    .screen-bars i:nth-child(2n) {{ animation-duration:540ms }}
    .screen-bars i:nth-child(3n) {{ animation-duration:920ms }}
    .screen-status {{ color:#ff5c1a; font:900 .56rem "Courier New",monospace; letter-spacing:.3em; margin-top:8px; opacity:.3; position:relative; z-index:4 }}
    @keyframes cursorBlink {{ 0%,45%{{opacity:1}} 50%,95%{{opacity:0}} 100%{{opacity:1}} }}
    @keyframes barPulse {{ from{{height:5px;opacity:.45}} to{{height:28px;opacity:1}} }}
    .shelf {{ background:#eee8d9; color:#181824; border:1px solid #d5cbb8; box-shadow:0 24px 60px #0008,inset 0 1px 0 #fff7; padding:26px 28px 32px 66px; position:relative }}
    .shelf::before {{ content:""; position:absolute; left:22px; top:0; bottom:0; width:14px; background:radial-gradient(circle,#08090c 0 5px,transparent 5.6px) center 28px/14px 48px repeat-y }}
    .shelf::after {{ content:""; position:absolute; left:50px; top:0; bottom:0; border-left:1px dotted #4a463f; opacity:.7 }}
    .create-card,.book-card {{ display:flex; justify-content:space-between; align-items:center; gap:18px; min-height:58px; padding:16px 18px; color:inherit; text-decoration:none; border-bottom:1px dotted #b8ad9d }}
    .create-card {{ background:#111116; color:#f0eadc; margin:0 0 12px; border:0; box-shadow:0 8px 0 #c7bda9; font-weight:900 }}
    .book-card:hover {{ background:#f7f2e7 }}
    .book-card:active,.create-card:active {{ transform:translateY(2px) }}
    .book-title {{ font-weight:900; line-height:1.45 }}
    time {{ color:#777063; white-space:nowrap; font-size:.86rem }}
    .empty {{ color:#777063; padding:24px 0; line-height:1.7 }}
    @media (max-width:640px) {{
      main {{ width:100%; padding:18px 10px 60px }}
      .topbar {{ padding:0 4px }}
      .shelf {{ padding:22px 18px 28px 54px }}
      .book-card,.create-card {{ align-items:flex-start; flex-direction:column; gap:4px }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="topbar">
      <div class="brand"><span class="brand-mark">PI</span><span>播客研究所</span></div>
      <a class="home-link" href="/">返回研究所</a>
    </div>
    <section class="hero">
      <div class="screen">
        <div class="eyebrow">OUTPUT ARCHIVE</div>
        <h1>网页书架<span>▮</span></h1>
        <p>ARCHIVE READY · 已生成的专题都在这里</p>
        <div class="screen-bars" aria-hidden="true">
          <i style="height:16px"></i><i style="height:28px"></i><i style="height:10px"></i><i style="height:22px"></i><i style="height:34px"></i>
          <i style="height:14px"></i><i style="height:26px"></i><i style="height:18px"></i><i style="height:31px"></i><i style="height:12px"></i>
          <i style="height:24px"></i><i style="height:19px"></i><i style="height:30px"></i><i style="height:13px"></i><i style="height:27px"></i>
          <i style="height:17px"></i><i style="height:21px"></i><i style="height:29px"></i><i style="height:11px"></i><i style="height:25px"></i>
        </div>
        <div class="screen-status">── ALL SYSTEMS OK ──</div>
      </div>
    </section>
    <section class="shelf">
      <a class="create-card" href="/"><span>＋ 创建新的播客专题</span><time>本地助手</time></a>
      {cards}
    </section>
  </main>
</body>
</html>"""
    return html.encode("utf-8")


def safe_cache_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .") or "episode"


def extract_episode_urls(value: str) -> list[str]:
    matches = re.findall(
        r"https?://(?:www\.)?xiaoyuzhoufm\.com/episode/[A-Za-z0-9]+",
        value,
    )
    seen, urls = set(), []
    for url in matches:
        normalized = url.replace("http://", "https://")
        if normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return urls


def episode_id_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def parse_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not value:
        return 0
    text = str(value).replace(",", "").strip()
    multiplier = 1
    if text.endswith("万"):
        multiplier = 10_000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


def recency_days(value: str) -> int | None:
    return None if value == "any" else int(value)


def select_episodes(
    candidates: list[dict],
    count: int,
    recency: str = "180",
    *,
    max_per_podcast: int = 2,
) -> list[dict]:
    """Select episodes deterministically by freshness, heat, and podcast diversity."""
    if count <= 0:
        return []
    window = recency_days(recency)

    def parse_date(candidate: dict) -> datetime | None:
        raw = candidate.get("date") or candidate.get("pub_date") or candidate.get("pubDate")
        if not raw:
            return None
        text = str(raw).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            try:
                return datetime.strptime(str(raw)[:10], "%Y-%m-%d")
            except ValueError:
                return None

    today = datetime.now().astimezone()
    scored = []
    for order, candidate in enumerate(candidates):
        date = parse_date(candidate)
        age_days = None
        if date:
            if date.tzinfo is None:
                date = date.astimezone()
            age_days = max((today - date).days, 0)
        freshness = 0
        if age_days is None:
            freshness = 8
        elif window is None:
            freshness = max(0, 40 - min(age_days, 720) / 18)
        elif age_days <= window:
            freshness = 60 - (age_days / max(window, 1)) * 20
        else:
            freshness = max(0, 18 - (age_days - window) / 30)
        heat = min(parse_int(candidate.get("play_count") or candidate.get("plays")), 500_000) / 20_000
        relevance = float(candidate.get("relevance", 0) or 0)
        score = relevance * 20 + freshness + heat
        scored.append((score, -order, candidate))

    selected, podcast_counts = [], {}
    for _, _, candidate in sorted(scored, reverse=True):
        podcast = str(candidate.get("podcast") or "").strip()
        if podcast and podcast_counts.get(podcast, 0) >= max_per_podcast:
            continue
        selected.append(candidate)
        if podcast:
            podcast_counts[podcast] = podcast_counts.get(podcast, 0) + 1
        if len(selected) >= count:
            break
    if len(selected) < min(count, len(candidates)):
        for _, _, candidate in sorted(scored, reverse=True):
            if candidate in selected:
                continue
            selected.append(candidate)
            if len(selected) >= count:
                break
    return selected


def cache_user_paste_if_possible(text: str) -> dict | None:
    episode_ids = sorted(
        set(re.findall(r"xiaoyuzhoufm\.com/episode/([A-Za-z0-9]+)", text))
    )
    if len(episode_ids) != 1:
        return None
    episode_id = episode_ids[0]
    target = TRANSCRIPT_CACHE / safe_cache_name(episode_id)
    target.mkdir(parents=True, exist_ok=True)
    transcript = {
        "text": text,
        "segments": [{"start": 0, "end": None, "text": text}],
        "source": "user-paste",
    }
    (target / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest = {
        "episode_id": episode_id,
        "source": "user-paste",
        "cached_at": now(),
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def latest_output_after(timestamp: str | None) -> Path | None:
    threshold = 0.0
    if timestamp:
        try:
            threshold = datetime.fromisoformat(timestamp).timestamp()
        except ValueError:
            pass
    outputs = [
        path
        for path in LIBRARY.glob("*.html")
        if path.name != "index.html" and path.stat().st_mtime >= threshold - 2
    ]
    return max(outputs, key=lambda path: path.stat().st_mtime) if outputs else None


def normalize_for_match(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


def ngrams(value: str, size: int = 2) -> set[str]:
    return {value[index : index + size] for index in range(max(len(value) - size + 1, 0))}


def find_existing_output(job: dict) -> Path | None:
    """Find a previously generated bookshelf HTML for a similar quick request."""
    if job.get("production_mode", "quick") != "quick":
        return None
    query = normalize_for_match(str(job.get("request", "")))
    if len(query) < 4:
        return None
    candidates = []
    for path in LIBRARY.glob("*.html"):
        if path.name == "index.html":
            continue
        name = normalize_for_match(path.stem)
        if not name:
            continue
        score = 0
        if query in name or name in query:
            score = min(len(query), len(name)) + 20
        else:
            shared_bigrams = ngrams(query, 2) & ngrams(name, 2)
            shared_trigrams = ngrams(query, 3) & ngrams(name, 3)
            score = len(shared_bigrams) * 2 + len(shared_trigrams) * 3
        if score >= 6:
            candidates.append((score, path.stat().st_mtime, path))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def find_project_dir(job: dict) -> Path | None:
    """Find the 专题项目 directory whose name best matches this job's request."""
    projects_root = ROOT / "专题项目"
    if not projects_root.exists():
        return None
    query = normalize_for_match(str(job.get("request", "")))
    if len(query) < 3:
        return None
    best, best_score = None, 0
    for path in projects_root.iterdir():
        if not path.is_dir():
            continue
        name = normalize_for_match(path.name)
        if not name:
            continue
        if query in name or name in query:
            score = min(len(query), len(name)) + 20
        else:
            score = len(ngrams(query, 2) & ngrams(name, 2)) * 2
        if score > best_score:
            best, best_score = path, score
    return best if best_score >= 6 else None


def selected_episodes(job: dict) -> list[dict]:
    """Parse a project's chosen episodes (id / title / podcast) for the front end."""
    project = find_project_dir(job)
    if not project:
        return []
    episodes_file = project / "episodes.txt"
    if not episodes_file.exists():
        return []
    urls = [
        line.strip()
        for line in episodes_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and "xiaoyuzhoufm.com/episode/" in line
    ]
    # 从候选单集.md 里抓每个 episode_id 对应的标题和节目名（✅ 入选行）。
    meta = {}
    candidates = project / "候选单集.md"
    if candidates.exists():
        for line in candidates.read_text(encoding="utf-8").splitlines():
            m = re.search(r"\[([^\]]+)\]\((https://www\.xiaoyuzhoufm\.com/episode/([a-f0-9]+))\)", line)
            if not m:
                continue
            # 标题里可能含 `|`，不能按固定列号取节目名；改取“链接之后”的第一个单元格。
            after = line[m.end():]
            tail_cells = [c.strip() for c in after.split("|") if c.strip()]
            podcast = tail_cells[0] if tail_cells else ""
            # 标题去掉自身可能带的 `节目名 | ` 前缀噪声，只保留最后一段为主标题。
            title = m.group(1).strip()
            meta[m.group(3)] = {"title": title, "podcast": podcast}
    result = []
    for url in urls:
        eid = url.rstrip("/").rsplit("/", 1)[-1]
        info = meta.get(eid, {})
        result.append(
            {
                "episode_id": eid,
                "url": url,
                "title": info.get("title", url),
                "podcast": info.get("podcast", ""),
            }
        )
    return result


def load_pipeline_module():
    spec = importlib.util.spec_from_file_location("xiaoyuzhou_pipeline", PIPELINE_SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载 pipeline.py：{PIPELINE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def markdown_cell(value: object) -> str:
    return str(value or "").replace("|", "｜").replace("\n", " ").strip()


def transcript_source_for_episode(episode_id: str) -> tuple[str, str]:
    manifest = TRANSCRIPT_CACHE / safe_cache_name(episode_id) / "manifest.json"
    transcript = TRANSCRIPT_CACHE / safe_cache_name(episode_id) / "transcript.json"
    if transcript.exists():
        label = "听稿依据"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
            source = data.get("source")
            if source == "user-paste":
                label = "用户文稿"
        except (OSError, json.JSONDecodeError):
            pass
        return "source-audio", label
    return "source-intro", "简介依据"


def source_badge(source: str) -> str:
    normalized = (source or "intro").strip().lower()
    if normalized in {"audio", "transcript", "user-paste", "听稿"}:
        return '<span class="source-badge source-audio">🎧 听稿依据</span>'
    if normalized in {"web", "核验"}:
        return '<span class="source-badge source-web">🌐 联网核验</span>'
    if normalized in {"infer", "inference", "推断"}:
        return '<span class="source-badge source-infer">🤔 AI 推断</span>'
    return '<span class="source-badge source-intro">📝 简介依据</span>'


def source_kind_from_class(source_class: str) -> str:
    if source_class == "source-audio":
        return "audio"
    if source_class == "source-web":
        return "web"
    if source_class == "source-infer":
        return "infer"
    return "intro"


def relation_marker(relation: str) -> str:
    normalized = (relation or "").strip().lower()
    mapping = {
        "consensus": "✅共识",
        "共识": "✅共识",
        "divergence": "⚠️分歧",
        "difference": "⚠️分歧",
        "分歧": "⚠️分歧",
        "partial": "◐部分分歧",
        "部分分歧": "◐部分分歧",
        "unique": "💡独有",
        "独有": "💡独有",
        "conflict": "❓信息冲突",
        "信息冲突": "❓信息冲突",
    }
    return mapping.get(normalized, "◐部分分歧")


def transcript_excerpt(episode_id: str, limit: int = 1800) -> str:
    transcript = TRANSCRIPT_CACHE / safe_cache_name(episode_id) / "transcript.json"
    if not transcript.exists():
        return ""
    try:
        data = json.loads(transcript.read_text(encoding="utf-8"))
        text_value = data.get("text") or " ".join(
            str(segment.get("text", "")) for segment in data.get("segments", [])
        )
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""
    text_value = re.sub(r"\s+", " ", str(text_value)).strip()
    return text_value[:limit]


def strip_html_tags(value: str) -> str:
    text_value = re.sub(r"<[^>]+>", " ", value or "")
    text_value = re.sub(r"\s+", " ", text_value)
    return text_value.strip()


def episode_angle(title: str) -> str:
    cleaned = re.sub(r"^#?\d+\s*", "", title or "").strip()
    for separator in ("：", ":", "｜", "|", "，", ","):
        if separator in cleaned:
            part = cleaned.split(separator, 1)[0].strip()
            if 4 <= len(part) <= 22:
                return part
    return cleaned[:18] or "这一期的回答"


def candidate_from_episode(episode) -> dict:
    source_class, source_label = transcript_source_for_episode(episode.eid)
    description = strip_html_tags(episode.description or episode.shownotes_html)
    return {
        "episode_id": episode.eid,
        "url": episode.url,
        "title": episode.title,
        "podcast": episode.podcast,
        "date": episode.pub_date[:10] if episode.pub_date else "",
        "duration": episode.duration,
        "description": description,
        "shownotes": strip_html_tags(episode.shownotes_html),
        "source_class": source_class,
        "source_label": source_label,
        "angle": episode_angle(episode.title),
        "play_count": None,
    }


def fetch_link_candidates(urls: list[str], log) -> list[dict]:
    pipeline = load_pipeline_module()
    candidates = []
    for index, url in enumerate(urls, 1):
        log.write(f"[{now()}] 抓取公开简介 {index}/{len(urls)}：{url}\n")
        log.flush()
        episode = pipeline.parse_episode(url)
        candidates.append(candidate_from_episode(episode))
    return candidates


def comparison_payload(selected: list[dict], title: str) -> dict:
    episodes = []
    for item in selected:
        episode_id = str(item.get("episode_id", ""))
        episodes.append(
            {
                "episode_id": episode_id,
                "title": item.get("title", ""),
                "podcast": item.get("podcast", ""),
                "url": item.get("url", ""),
                "date": item.get("date", ""),
                "source": source_kind_from_class(str(item.get("source_class", ""))),
                "angle_hint": item.get("angle", ""),
                "description": truncate_plain(item.get("description") or item.get("shownotes") or "", 1800),
                "transcript_excerpt": transcript_excerpt(episode_id),
            }
        )
    return {"title": title, "episodes": episodes}


def truncate_plain(value: object, limit: int) -> str:
    text_value = re.sub(r"\s+", " ", str(value or "")).strip()
    return text_value[:limit]


def extract_json_object(value: str) -> dict:
    text_value = value.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text_value, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    decoder = json.JSONDecoder()
    for index, char in enumerate(text_value):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text_value[index:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("模型输出中没有可解析 JSON 对象")


def structured_comparison_prompt(payload: dict) -> str:
    return f"""你是播客专题的结构化比对器。只基于输入材料工作，不联网、不写文件、不输出 Markdown。

任务：把多期播客材料拆成 claim 和 subtopic，用于速读版观点矩阵。

硬规则：
- 只输出一个 JSON object，不要解释，不要代码块。
- 不要发明公司、人物、金额、事实或节目原话。
- source 为 audio 的材料可以写“听稿依据”；source 为 intro 的材料只能写“简介依据”，不能写成主播已经明确说过。
- 如果信息不足，写成“简介显示/材料显示/尚待全文确认”。
- claims 6 到 12 条即可，subtopics 3 到 5 条即可。
- 每个 summary 尽量 20-45 个中文字符。

JSON schema：
{{
  "claims": [
    {{
      "claim_id": "C01",
      "episode_id": "string",
      "podcast": "string",
      "speaker": "节目简介|听稿片段|未解析",
      "text": "原子观点，<=60字",
      "evidence": "依据摘要，<=90字",
      "source": "audio|intro|web|infer",
      "confidence": "高|中|低",
      "topic_hint": "短议题"
    }}
  ],
  "subtopics": [
    {{
      "subtopic_id": "S01",
      "title": "子议题标题，<=16字",
      "relation": "consensus|divergence|partial|unique|conflict",
      "positions": [
        {{
          "episode_id": "string",
          "summary": "该期在此议题上的立场，<=55字",
          "source": "audio|intro|web|infer",
          "claim_ids": ["C01"]
        }}
      ],
      "note": "这一行为什么重要，<=80字"
    }}
  ]
}}

输入：
{json.dumps(payload, ensure_ascii=False)}
"""


def run_structured_comparison(selected: list[dict], title: str, log) -> dict:
    if COMPARISON_ENGINE in {"off", "none", "deterministic"}:
        raise RuntimeError("结构化比对模型调用已关闭")
    payload = comparison_payload(selected, title)
    command = [
        str(CODEX),
        "exec",
        "--skip-git-repo-check",
        "-C",
        str(ROOT),
        "-s",
        "danger-full-access",
        "-c",
        'approval_policy="never"',
        "-c",
        'model_reasoning_effort="low"',
        "--color",
        "never",
        structured_comparison_prompt(payload),
    ]
    log.write(f"[{now()}] 结构化比对：启动一次模型调用\n")
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        log.write((result.stderr or result.stdout)[-4000:])
        raise RuntimeError(f"结构化比对失败：{result.stderr or result.stdout}")
    data = extract_json_object(result.stdout)
    if not isinstance(data.get("claims"), list) or not isinstance(data.get("subtopics"), list):
        raise RuntimeError("结构化比对 JSON 缺少 claims/subtopics")
    log.write(
        f"[{now()}] 结构化比对：返回 {len(data.get('claims', []))} 条观点、"
        f"{len(data.get('subtopics', []))} 个子议题\n"
    )
    return data


def normalize_claims(raw_claims: object) -> list[dict]:
    claims = raw_claims if isinstance(raw_claims, list) else []
    normalized = []
    for index, item in enumerate(claims, 1):
        if not isinstance(item, dict):
            continue
        claim_id = truncate_plain(item.get("claim_id") or f"C{index:02d}", 12)
        normalized.append(
            {
                "claim_id": claim_id,
                "episode_id": truncate_plain(item.get("episode_id"), 80),
                "podcast": truncate_plain(item.get("podcast"), 40),
                "speaker": truncate_plain(item.get("speaker") or "未解析", 20),
                "text": truncate_plain(item.get("text"), 80),
                "evidence": truncate_plain(item.get("evidence"), 110),
                "source": truncate_plain(item.get("source") or "intro", 12),
                "confidence": truncate_plain(item.get("confidence") or "低", 8),
                "topic_hint": truncate_plain(item.get("topic_hint") or "观点线索", 24),
            }
        )
    return normalized


def normalize_subtopics(raw_subtopics: object) -> list[dict]:
    subtopics = raw_subtopics if isinstance(raw_subtopics, list) else []
    normalized = []
    for index, item in enumerate(subtopics, 1):
        if not isinstance(item, dict):
            continue
        positions = []
        raw_positions = item.get("positions") if isinstance(item.get("positions"), list) else []
        for raw_position in raw_positions:
            if not isinstance(raw_position, dict):
                continue
            claim_ids = raw_position.get("claim_ids")
            if not isinstance(claim_ids, list):
                claim_ids = []
            positions.append(
                {
                    "episode_id": truncate_plain(raw_position.get("episode_id"), 80),
                    "summary": truncate_plain(raw_position.get("summary"), 70),
                    "source": truncate_plain(raw_position.get("source") or "intro", 12),
                    "claim_ids": [truncate_plain(claim_id, 12) for claim_id in claim_ids],
                }
            )
        normalized.append(
            {
                "subtopic_id": truncate_plain(item.get("subtopic_id") or f"S{index:02d}", 12),
                "title": truncate_plain(item.get("title") or f"子议题 {index}", 24),
                "relation": truncate_plain(item.get("relation") or "partial", 20),
                "positions": positions,
                "note": truncate_plain(item.get("note"), 100),
            }
        )
    return normalized


def render_structured_comparison_files(
    project: Path,
    title: str,
    selected: list[dict],
    comparison: dict,
) -> None:
    claims = normalize_claims(comparison.get("claims"))
    subtopics = normalize_subtopics(comparison.get("subtopics"))
    selected_by_id = {str(item.get("episode_id", "")): item for item in selected}
    audio_count = sum(1 for item in selected if item.get("source_class") == "source-audio")
    intro_count = len(selected) - audio_count

    atom_rows = [
        "| claim_id | 单集/来源 | 说话人 | 原子观点 | 支撑原话或依据 | source | confidence | topic_hint |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for claim in claims:
        episode = selected_by_id.get(claim["episode_id"], {})
        episode_title = episode.get("title") or claim.get("podcast") or claim.get("episode_id")
        atom_rows.append(
            "| "
            f"{markdown_cell(claim['claim_id'])} | "
            f"{markdown_cell(episode_title)} | "
            f"{markdown_cell(claim['speaker'])} | "
            f"{markdown_cell(claim['text'])} | "
            f"{markdown_cell(claim['evidence'])} | "
            f"{markdown_cell(claim['source'])} | "
            f"{markdown_cell(claim['confidence'])} | "
            f"{markdown_cell(claim['topic_hint'])} |"
        )
    if not claims:
        atom_rows.append("| C00 | 未生成 | 未解析 | 结构化比对没有返回可用观点 | — | infer | 低 | 回退线索 |")
    (project / "观点原子.md").write_text("\n".join(atom_rows) + "\n", encoding="utf-8")

    headers = [
        f"{markdown_cell(item.get('podcast') or '播客')}：{markdown_cell(item.get('angle') or item.get('title'))}"
        for item in selected
    ]
    matrix = [
        "| 子议题 | " + " | ".join(headers) + " |",
        "|---|" + "|".join("---" for _ in headers) + "|",
    ]
    for subtopic in subtopics:
        positions_by_episode = {
            str(position.get("episode_id", "")): position
            for position in subtopic["positions"]
            if position.get("episode_id")
        }
        cells = []
        for item in selected:
            position = positions_by_episode.get(str(item.get("episode_id", "")))
            if not position:
                cells.append("—未提及")
                continue
            marker = relation_marker(subtopic["relation"])
            badge = source_badge(position.get("source"))
            cells.append(f"{marker} {badge} {markdown_cell(position.get('summary'))}")
        matrix.append(f"| {markdown_cell(subtopic['title'])} | " + " | ".join(cells) + " |")

    relation_groups = {
        "大家争的是什么": {"divergence", "partial", "conflict", "分歧", "部分分歧", "信息冲突"},
        "没争议的共识": {"consensus", "共识"},
        "只有某一期提到的独到观点": {"unique", "独有"},
    }
    sections = {}
    for section_title, relation_names in relation_groups.items():
        lines = []
        for subtopic in subtopics:
            if subtopic["relation"].strip().lower() not in relation_names:
                continue
            marker = relation_marker(subtopic["relation"])
            note = markdown_cell(subtopic.get("note") or "模型从现有材料中归纳出的关系。")
            lines.append(f"- {marker} **{markdown_cell(subtopic['title'])}**：{note} <span class=\"source-badge source-infer\">🤔 AI 推断</span>")
        sections[section_title] = "\n".join(lines) if lines else "- 暂无足够材料判断。"

    episode_lines = []
    for item in selected:
        badge = source_badge(source_kind_from_class(str(item.get("source_class", ""))))
        summary = markdown_cell(item.get("description") or item.get("shownotes") or item.get("title"))
        episode_lines.append(
            f"- **{markdown_cell(item.get('podcast'))}｜{markdown_cell(item.get('title'))}**："
            f"{summary[:120]} {badge}"
        )

    comparison_md = f"""# 观点地图：{title}

本版用一次结构化比对，把多期节目拆成观点原子，再合并成子议题矩阵。它优先复用已有听稿；没有听稿时只使用公开简介，不把简介改写成主播原话。

## 依据构成

- <span class="source-badge source-audio">🎧 听稿依据</span> {audio_count} 期：命中全局听稿或用户文稿缓存。
- <span class="source-badge source-intro">📝 简介依据</span> {intro_count} 期：来自公开节目页、简介或 show notes。
- <span class="source-badge source-infer">🤔 AI 推断</span> 仅用于跨集关系判断，不作为事实来源。

## 观点矩阵

{chr(10).join(matrix)}

## 大家争的是什么

{sections["大家争的是什么"]}

## 没争议的共识

{sections["没争议的共识"]}

## 只有某一期提到的独到观点

{sections["只有某一期提到的独到观点"]}

## 需要存疑的地方

- 简介依据只能说明节目公开介绍里出现了相关方向，不能等同于主播完整观点。
- 公司、人物、金额、时间和现代事件，对外引用前仍需要回听或联网核验。
- 如果要做精读版，应先补全文听稿，再重新生成观点矩阵。

## 各期速览

{chr(10).join(episode_lines)}
"""
    (project / "跨集比对.md").write_text(comparison_md, encoding="utf-8")


def quick_project_title(job: dict, selected: list[dict]) -> str:
    if job.get("mode") == "topic":
        return str(job.get("request", "")).strip()[:40] or "播客速读专题"
    if len(selected) == 1:
        return f"播客速读：{selected[0].get('title') or '单集专题'}"
    podcasts = [item.get("podcast") for item in selected if item.get("podcast")]
    if len(set(podcasts)) == 1 and podcasts:
        return f"{podcasts[0]}：播客速读专题"
    return "播客速读：多期观点地图"


def write_quick_project_files(project: Path, title: str, selected: list[dict]) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "文章").mkdir(exist_ok=True)
    (project / "episodes.txt").write_text(
        "\n".join(str(item["url"]) for item in selected) + "\n",
        encoding="utf-8",
    )
    candidates_rows = [
        "| 入选 | 单集 | 节目 | 日期 | 来源 | 入选理由 |",
        "|---|---|---|---|---|---|",
    ]
    for item in selected:
        candidates_rows.append(
            "| ✅ | "
            f"[{markdown_cell(item.get('title'))}]({item.get('url')}) | "
            f"{markdown_cell(item.get('podcast'))} | "
            f"{markdown_cell(item.get('date') or '未公开/未检索到')} | "
            f"{markdown_cell(item.get('source_label'))} | "
            f"链接模式指定；阶段 1 管线只使用公开简介和已有缓存，不下载音频。 |"
        )
    (project / "候选单集.md").write_text(
        "# 候选单集\n\n" + "\n".join(candidates_rows) + "\n",
        encoding="utf-8",
    )

    audio_count = sum(1 for item in selected if item.get("source_class") == "source-audio")
    intro_count = len(selected) - audio_count
    headers = [
        f"{markdown_cell(item.get('podcast') or '播客')}：{markdown_cell(item.get('angle'))}"
        for item in selected
    ]
    main_cells = []
    risk_cells = []
    for item in selected:
        badge = (
            '<span class="source-badge source-audio">🎧 听稿依据</span>'
            if item.get("source_class") == "source-audio"
            else '<span class="source-badge source-intro">📝 简介依据</span>'
        )
        summary = item.get("description") or item.get("shownotes") or item.get("title")
        main_cells.append(f"💡独有 {badge} {markdown_cell(summary)[:64]}。")
        risk_cells.append(f"❓信息冲突 {badge} 阶段 1 未做完整跨集推理，只能作为速读线索。")
    matrix = [
        "| 子议题 | " + " | ".join(headers) + " |",
        "|---|" + "|".join("---" for _ in headers) + "|",
        "| 这期主要回答什么 | " + " | ".join(main_cells) + " |",
        "| 需要继续坐实 | " + " | ".join(risk_cells) + " |",
    ]
    comparison = f"""# 观点地图：{title}

本版基于 {audio_count} 期听稿依据、{intro_count} 期简介依据整理；阶段 1 速读管线不下载音频、不运行 ASR、不写长文。

## 依据构成

- <span class="source-badge source-audio">🎧 听稿依据</span> {audio_count} 期：命中全局听稿或用户文稿缓存。
- <span class="source-badge source-intro">📝 简介依据</span> {intro_count} 期：来自公开节目页、简介或 show notes。
- <span class="source-badge source-infer">🤔 AI 推断</span> 本阶段暂不做开放式长文推断；只生成可回退的最简观点地图。

## 观点矩阵

{chr(10).join(matrix)}

## 大家争的是什么

阶段 1 暂不判断真实分歧，只把每期节目可公开确认的回答方向放到同一张地图里。后续阶段 2 会用一次结构化模型调用补全 claim/subtopic，再判断共识、分歧和独有观点。<span class="source-badge source-infer">🤔 AI 推断</span>

## 没争议的共识

这些单集都由用户在链接模式中指定，说明它们至少共同服务于同一个研究问题或阅读专题。<span class="source-badge source-infer">🤔 AI 推断</span>

## 只有某一期提到的独到观点

{chr(10).join(f'- **{markdown_cell(item.get("title"))}**：{markdown_cell(item.get("description") or item.get("shownotes") or "公开简介暂未展开")} <span class="source-badge {item.get("source_class")}">{ "🎧 听稿依据" if item.get("source_class") == "source-audio" else "📝 简介依据"}</span>' for item in selected)}

## 需要存疑的地方

阶段 1 没有全文转写，也没有做事实核验。公司、人物、金额、时间和节目观点在对外使用前需要回听或补精读。<span class="source-badge source-infer">🤔 AI 推断</span>

## 各期速览

{chr(10).join(f'- **{markdown_cell(item.get("podcast"))}｜{markdown_cell(item.get("title"))}**：{markdown_cell(item.get("angle"))}。' for item in selected)}
"""
    (project / "跨集比对.md").write_text(comparison, encoding="utf-8")
    atoms = [
        "| claim_id | 单集/来源 | 说话人 | 原子观点 | 支撑原话或依据 | source | confidence | topic_hint |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for index, item in enumerate(selected, 1):
        source = "audio" if item.get("source_class") == "source-audio" else "intro"
        atoms.append(
            f"| C{index:02d} | {markdown_cell(item.get('title'))} | 未解析 | "
            f"{markdown_cell(item.get('angle'))} | "
            f"{markdown_cell(item.get('description') or item.get('shownotes') or item.get('title'))[:80]} | "
            f"{source} | {'中' if source == 'audio' else '低'} | 速读线索 |"
        )
    (project / "观点原子.md").write_text("\n".join(atoms) + "\n", encoding="utf-8")
    article = f"""# 速读说明：{title}

这是阶段 1 管线生成的速读版：它的价值是先把用户给出的几期节目放进同一张观点地图，帮助你判断哪一期值得继续补全文。

## 现在可以怎么读

先看上方观点地图，确认这组节目大致分成哪些回答路径；如果某条路径值得深挖，再点击“补全文”或后续升级为精读版。

## 当前限制

本版没有运行 ASR，也没有让 agent 写长文。它不会把简介中的判断伪装成嘉宾原话；所有需要精确引用的内容，都应该在补全文后再使用。
"""
    (project / "文章" / "01-速读说明.md").write_text(article, encoding="utf-8")


def build_project_html(project: Path, title: str, log) -> Path:
    if shutil.which("uv") and os.environ.get("PODCAST_DISABLE_UV") != "1":
        command = ["uv", "run", "python", str(PIPELINE_SCRIPT)]
    else:
        command = [sys.executable, str(PIPELINE_SCRIPT)]
    command += ["build-html", "--project", str(project), "--title", title]
    env = {
        **os.environ,
        "UV_PROJECT_ENVIRONMENT": str(
            Path("~/Library/Caches/xiaoyuzhou-illustrated-ebook-venv").expanduser()
        ),
    }
    result = subprocess.run(
        command,
        cwd=PIPELINE_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    log.write(result.stdout)
    log.write(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"build-html 失败：{result.stderr or result.stdout}")
    output = output_from_log(result.stdout + result.stderr)
    if not output:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip().endswith(".html")]
        if lines:
            output = Path(lines[-1])
    if not output or not output.exists():
        raise RuntimeError("build-html 未返回有效 HTML 路径")
    return output


def run_quick_pipeline(job: dict, log) -> Path:
    if job.get("mode") != "links":
        raise RuntimeError("阶段 1 的 pipeline quick 仅支持链接模式；主题模式仍走旧 agent。")
    urls = extract_episode_urls(str(job.get("request", "")))
    if not urls:
        raise RuntimeError("链接模式没有可用的小宇宙单集链接")
    candidates = fetch_link_candidates(urls, log)
    selected = select_episodes(
        candidates,
        min(int(job.get("episode_count", len(candidates))), len(candidates)),
        str(job.get("recency", "180")),
    )
    if not selected:
        raise RuntimeError("没有选出可用单集")
    title = quick_project_title(job, selected)
    project = ROOT / "专题项目" / safe_cache_name(title)
    log.write(f"[{now()}] 写入速读项目：{project}\n")
    write_quick_project_files(project, title, selected)
    try:
        comparison = run_structured_comparison(selected, title, log)
        render_structured_comparison_files(project, title, selected, comparison)
        job["comparison_engine"] = COMPARISON_ENGINE
        log.write(f"[{now()}] 结构化比对完成，已更新观点地图\n")
    except Exception as exc:  # noqa: BLE001
        job["comparison_engine"] = "fallback"
        job["comparison_error"] = str(exc)
        log.write(f"[{now()}] 结构化比对跳过/失败，回退到最简观点地图：{exc}\n")
    log.write(f"[{now()}] 构建 HTML\n")
    output = build_project_html(project, title, log)
    job["project"] = str(project)
    job["quick_engine"] = "pipeline"
    return output


def output_from_log(log_text: str) -> Path | None:
    matches = re.findall(r"/Users/biu/[^\n`]+?\.html", log_text)
    for raw in reversed(matches):
        path = Path(raw.strip())
        if path.exists() and path.parent.resolve() == LIBRARY.resolve():
            return path
    return None


def public_job(job: dict) -> dict:
    result = dict(job)
    output = result.get("output")
    if output:
        output_path = Path(output)
        if output_path.exists() and output_path.parent.resolve() == LIBRARY.resolve():
            result["output_url"] = f"/library/{quote(output_path.name)}"
    return result


def build_prompt(job: dict) -> str:
    count = job["episode_count"]
    production_mode = job.get("production_mode", "quick")
    if job["mode"] == "topic":
        request = f"""
用户研究主题：
{job["request"]}

这是主题模式。请先检索公开网页，初筛 12–20 期候选，优先主题相关、播放量高、
节目订阅量高、发布时间新且有一手经验的内容，再自动选出 {count} 期观点互补的节目。
"""
    elif job["mode"] == "links":
        request = f"""
用户提供的小宇宙单集链接：
{job["request"]}

这是链接模式。请处理全部有效链接；如果链接之间存在共同主题，将它们编辑成一个专题。
"""
    else:
        return f"""执行一个本地“小宇宙文稿专题”出版任务。可参考
$xiaoyuzhou-illustrated-ebook 的编辑规范和 HTML 构建脚本，但不要启动转写流程，
不要调用腾讯云 ASR，不要运行本地 Whisper，也不要调用 $ian-xiaohei-illustrations。

用户粘贴的小宇宙文稿如下：
{job["request"]}

这是文稿模式，目标是基于用户已经从小宇宙 App 复制出的文稿，快速生成可阅读专题：
- 先在 {ROOT / "专题项目"} 下创建一个独立专题目录；
- 将用户粘贴的原始内容完整保存为 `<项目>/原始文稿/小宇宙文稿.md`，作为素材留档；
- 如果能从文本或链接解析出小宇宙 episode_id，请同时把这份文稿写入全局转写缓存：
  `{TRANSCRIPT_CACHE}/<episode_id>/transcript.json`，并写入 manifest.json，source 标为 `user-paste`；
- 如果文本里包含标题、单集链接、时间戳或多段文稿，请尽量解析并保留来源；
- 不下载音频，不转写，不调用任何云端 ASR，因此不消耗腾讯云额度；
- 正文必须重新组织为文章，不能把逐字稿直接贴进去；
- 去除口头语、重复、无意义寒暄和密集时间戳；
- 保留关键论点、案例、因果关系和必要原话；
- 公司、人物、金额、产品参数和现代事件需要联网核验；
- 节目观点必须标明为嘉宾或主播观点，不写成已证实事实；
- 正文中对关键信息加来源徽标：
  `<span class="source-badge source-audio">🎧 听稿依据</span>` 用于用户粘贴文稿；
  `<span class="source-badge source-web">🌐 联网核验</span>` 用于公开网页核验；
  `<span class="source-badge source-infer">🤔 AI 推断</span>` 用于系统分析；
- 如果文稿内容不足以支撑某个判断，要明确写“文稿中未充分展开”，不要脑补；
- 生成 1 篇结构清晰的专题文章；如果粘贴了多期文稿，可增加横向对照；
- 必须执行下面的跨集比对要求；如果只有一篇文稿，就生成单集观点结构：
{CROSS_EPISODE_COMPARISON}
- 第一版不生成图片，优先完成文字和 HTML；
- 使用 pipeline.py 的 build-html 能力生成单文件 HTML；
- 最终 HTML 自动归档到 {LIBRARY}，并更新书架；
- HTML 需要支持书内搜索、选中文字联网检索和生成分享金句图；
- 生成 HTML 后只做必要文件校验，不截图、不启动浏览器，成功后立即结束。

完成后在最终回复中给出 HTML、书架、原始文稿目录和文章目录的绝对路径。
专题标题可由你根据文稿内容自动拟定。不要停在方案，直接完成成品。
"""

    recency = {
        "10": "优先最近 10 天，候选不足时可适当放宽，并明确说明。",
        "30": "优先最近一个月，候选不足时可适当放宽，并明确说明。",
        "90": "优先最近 90 天，经典高价值内容可以破例。",
        "180": "优先最近 180 天，兼顾仍然有效的经典内容。",
        "any": "不限制发布时间，但仍要记录并比较新鲜度。",
    }[job["recency"]]

    if production_mode == "quick":
        opening = """执行一个本地“播客速读版专题”出版任务。可参考
$xiaoyuzhou-illustrated-ebook 的编辑规范和 HTML 构建脚本，但不要启动完整 skill
工作流，也不要调用 $ian-xiaohei-illustrations。"""
        workflow = f"""
这是速读版模式，目标是尽快交付第一版：
- 先检查专题项目目录；如果已有候选单集.md、episodes.txt 或其他中间产物，必须直接复用，不要重新检索；
- 对每个入选单集先解析 episode_id，并检查全局转写缓存 `{TRANSCRIPT_CACHE}`；
- 如果某集已有 `transcript.json`，可以把它当作听稿依据使用，且不得重新转写；
- 没有缓存的单集，只使用公开单集页、Show Notes、节目介绍和可核验公开资料；
- 不下载音频，不运行 Whisper，不等待完整逐字转写；
- 将入选的 {count} 期内容整合成 1 篇结构清晰的专题文章，而不是逐期各写一篇；
- 清楚标注哪些信息来自节目简介，不能假装已经听完或完成全文转写；
- 正文必须带来源徽标：
  `<span class="source-badge source-audio">🎧 听稿依据</span>` 表示来自缓存转写/用户文稿；
  `<span class="source-badge source-intro">📝 简介依据</span>` 表示来自节目页或 Show Notes；
  `<span class="source-badge source-web">🌐 联网核验</span>` 表示来自公开网页核验；
  `<span class="source-badge source-infer">🤔 AI 推断</span>` 表示系统综合分析；
- 文章开头用一句话说明当前依据构成，例如“本版基于 X 期听稿依据、Y 期简介依据和联网核验整理”；
- 必须执行跨集比对要求，额外生成 `<项目>/观点原子.md` 和 `<项目>/跨集比对.md`；
- 第一版不生成图片；
- 优先完成候选报告、专题文章和可阅读 HTML。
- HTML 生成后只检查文件存在、标题和正文非空；不要截图，不要启动浏览器，
  不要做 Playwright/Chrome 视觉验收，也不要继续优化样式；
- 一旦 HTML 和书架写入成功，立即结束任务。
"""
    elif production_mode == "deep_one":
        target_episode = job.get("target_episode", "")
        opening = """请使用 $xiaoyuzhou-illustrated-ebook 为已有速读专题补全“某一期”的全文依据，
这是“单期补全文”模式：只转写用户指定的这一期音频，其余各期一律复用缓存或保持简介依据，
目的是用最少的云端 ASR 额度，把这一期从“简介依据”升级为“听稿依据”。"""
        workflow = f"""
这是单期补全文模式。**只对下面这一期做云端转写，绝对不要转写其它期：**
  目标单集标识：{target_episode}

- 先在 `{ROOT / "专题项目"}` 找到同主题已有项目目录，复用其 episodes.txt、候选单集.md、跨集比对.md，不要重新检索；
- 从 episodes.txt 中只挑出与“目标单集标识”匹配的那一行（按 episode_id 或链接片段匹配），
  生成一个只含这一行的临时 episodes 文件，例如 `<项目>/episodes.one.txt`；
- 必须先检查全局转写缓存 `{TRANSCRIPT_CACHE}`：如果这一期已有缓存，直接复用、不要重复提交 ASR；
- 只在 pipeline.py 目录对这一期运行：
  `UV_PROJECT_ENVIRONMENT="$HOME/Library/Caches/xiaoyuzhou-illustrated-ebook-venv" uv run python pipeline.py transcribe-auto --episodes "<项目>/episodes.one.txt" --cache "<缓存目录>"`；
- 优先 Groq Whisper Large v3 Turbo；若额度用尽/欠费/资源包不可用/无密钥，停止并明确说明原因，不要自动切本地 Whisper 长跑；
- 转写完成后缓存自动写入全局；这一期此后在所有专题中复用，永不重复转写；
- 然后用“更新后的缓存”重新生成本专题：这一期改用听稿依据，其余各期保持原有依据（简介/听稿）不变；
- 重写跨集比对时，这一期的观点必须基于全文听稿，并据此修正它在矩阵中的立场与原话；
- 正文必须带来源徽标（听稿/简介/核验/推断），如实反映每一期当前的依据强度，不得把仍是简介依据的期写成听稿；
- 必须重新生成 `<项目>/观点原子.md` 和 `<项目>/跨集比对.md`，再用 build-html 重出 HTML 并归档书架；
- 生成 HTML 后只做必要文件校验，不截图、不启动浏览器，成功后立即结束。
"""
    else:
        opening = """请使用 $xiaoyuzhou-illustrated-ebook 完成一个播客精读版专题。
使用已配置的云端 ASR 后端并行转写完整音频，正文配图仍按该 skill 的要求处理。"""
        workflow = f"""
这是精读版模式。优先复用缓存；实际耗时受音频总时长、Groq 每小时音频秒数额度、腾讯云资源包状态影响：
- 先检查专题项目目录；若已有同主题的候选单集.md、episodes.txt 和速读文章，
  直接复用选题与链接，不要重新进行候选检索；
- 必须先检查全局转写缓存 `{TRANSCRIPT_CACHE}`，已有缓存的单集不得重复提交 ASR；
- 如果尚未存在可读 HTML，先用公开资料和已有缓存生成 v1 初稿并归档，避免用户空等；
- 不要运行本地 Whisper，也不要先下载整段音频；
- 在 pipeline.py 所在目录运行：
  `UV_PROJECT_ENVIRONMENT="$HOME/Library/Caches/xiaoyuzhou-illustrated-ebook-venv" uv run python pipeline.py transcribe-auto --episodes "<项目>/episodes.txt" --cache "<缓存目录>"`；
- 该命令会优先复用全局缓存；若配置了 Groq API Key，优先使用 Groq Whisper Large v3 Turbo；否则使用腾讯云 ASR；
- 如果云端 ASR 返回免费额度用尽、余额不足、欠费、资源包不可用或没有配置密钥，必须停止任务并说明原因；
  不要自动切换到本地 Whisper 长时间运行，也不要只根据 Show Notes 冒充精读版；
- 必须读取全部转写结果后再写精读专题，不能只根据 Show Notes 扩写；
- 云端转写完成后，全局缓存会由 pipeline 自动写入；如果复用缓存，也要在文章依据说明中写清楚；
- 并行分析各期全文，再整合为观点对照、案例、分歧、适用边界和方法框架；
- 关键公司、人物、数据和现代事件仍需用可靠公开来源交叉核验；
- 正文必须带来源徽标：
  `<span class="source-badge source-audio">🎧 听稿依据</span>` 表示来自转写/文稿；
  `<span class="source-badge source-intro">📝 简介依据</span>` 表示来自节目页或 Show Notes；
  `<span class="source-badge source-web">🌐 联网核验</span>` 表示来自公开网页核验；
  `<span class="source-badge source-infer">🤔 AI 推断</span>` 表示系统综合分析；
- 优先完成完整文字专题；配图不得阻塞文字版交付；
- 必须执行跨集比对要求，额外生成 `<项目>/观点原子.md` 和 `<项目>/跨集比对.md`；
- 生成 HTML 后只做必要文件校验，不截图、不启动浏览器，成功后立即结束。
"""

    return f"""{opening}
{workflow}

{CROSS_EPISODE_COMPARISON}

{request}

筛选偏好：
- {recency}
- 热度和节目影响力是重要信号，但主题相关性优先；
- 同一档节目默认最多入选 2 期；
- 公开页面没有播放量、订阅量等数据时标注未公开，不得猜测；
- 公司、人物、金额、产品参数和现代事件必须联网核验；
- 节目观点必须标明为嘉宾或主播观点，不写成已证实事实。

交付要求：
1. 按上述快速或深度模式完成检索、候选报告、文章编辑和网页出版；
2. 保存项目素材到 {ROOT / "专题项目"} 下以专题命名的独立目录；
2.1 必须保存 `观点原子.md` 和 `跨集比对.md`；`跨集比对.md` 会自动排在 HTML 正文最前面；
3. 最终 HTML 自动归档到 {LIBRARY}，并更新书架；
4. HTML 需要支持书内搜索、选中文字联网检索和生成分享金句图；
5. 不生成 EPUB，除非任务确实需要；
6. 完成后在最终回复中给出 HTML、书架、候选报告和文章目录的绝对路径。

专题标题可由你根据内容自动拟定。不要停在方案或候选列表，直接完成成品。
"""


def run_job(job_id: str) -> None:
    job = load_job(job_id)
    if not job:
        return
    _, log_path = job_paths(job_id)
    job["status"] = "running"
    job["started_at"] = now()
    job["worker_pid"] = os.getpid()
    save_job(job)

    if (
        job.get("production_mode", "quick") == "quick"
        and not job.get("rerun_from_job_id")
    ):
        existing_output = find_existing_output(job)
        if existing_output:
            with log_path.open("w", encoding="utf-8") as log:
                log.write(f"[{now()}] 命中同主题已有速读专题，直接复用：{existing_output}\n")
            job["status"] = "completed"
            job["output"] = str(existing_output)
            job["message"] = "已命中同主题速读专题，直接打开已有阅读页。"
            job["finished_at"] = now()
            job.pop("codex_pid", None)
            job.pop("worker_pid", None)
            save_job(job)
            return

    if (
        job.get("production_mode", "quick") == "quick"
        and QUICK_ENGINE == "pipeline"
        and job.get("mode") == "links"
    ):
        try:
            with log_path.open("w", encoding="utf-8") as log:
                log.write(f"[{now()}] 开始阶段 1 pipeline 速读任务（链接模式）\n")
                output = run_quick_pipeline(job, log)
            job = load_job(job_id) or job
            job["status"] = "completed"
            job["output"] = str(output)
            job["return_code"] = 0
            job["message"] = "阶段 1 pipeline 速读专题已生成并保存到网页书架。"
            job["finished_at"] = now()
            job.pop("codex_pid", None)
            job.pop("worker_pid", None)
            save_job(job)
            return
        except Exception as exc:  # noqa: BLE001
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n[{now()}] pipeline 速读失败：{exc}\n")
            job = load_job(job_id) or job
            job["status"] = "failed"
            job["error"] = str(exc)
            job["finished_at"] = now()
            job.pop("codex_pid", None)
            job.pop("worker_pid", None)
            save_job(job)
            return

    command = [
        str(CODEX),
        "exec",
        "--skip-git-repo-check",
        "-C",
        str(ROOT),
        "--add-dir",
        str(SKILL_DIR),
        "-s",
        "danger-full-access",
        "-c",
        'approval_policy="never"',
        "--color",
        "never",
        build_prompt(job),
    ]
    if job.get("production_mode", "quick") in {"quick", "deep_one"}:
        command[command.index("--color"):command.index("--color")] = [
            "-c",
            'model_reasoning_effort="low"',
        ]
    elif job.get("production_mode") == "deep":
        command[command.index("--color"):command.index("--color")] = [
            "-c",
            'model_reasoning_effort="medium"',
        ]

    try:
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"[{now()}] 开始任务\n\n")
            log.flush()
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            job["codex_pid"] = process.pid
            save_job(job)
            if job.get("production_mode", "quick") == "quick":
                quick_output = None
                while True:
                    return_code = process.poll()
                    log.flush()
                    log_text = log_path.read_text(encoding="utf-8", errors="replace")
                    quick_output = latest_output_after(job.get("started_at")) or output_from_log(log_text)
                    if quick_output:
                        log.write(f"\n检测到速读 HTML 已生成，立即结束后台收尾：{quick_output}\n")
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        return_code = 0
                        break
                    if return_code is not None:
                        break
                    time.sleep(QUICK_OUTPUT_POLL_SECONDS)
            else:
                try:
                    return_code = process.wait(timeout=600)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    return_code = 124
                    log.write("\n精读版已达到 10 分钟时间上限，停止继续处理。\n")

        job = load_job(job_id) or job
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        output = latest_output_after(job.get("started_at")) or output_from_log(log_text)
        if return_code == 0:
            if output:
                job["status"] = "completed"
                job["output"] = str(output)
                if job.get("production_mode", "quick") == "quick":
                    job["message"] = "速读专题已生成并保存到网页书架。"
            else:
                job["status"] = "failed"
                if "ASR 不可用" in log_text or "Resource pack exhausted" in log_text:
                    job["message"] = "云端 ASR 未完成，尚未生成可阅读网页。请修复转写配置后继续制作。"
                else:
                    job["message"] = "任务结束但没有生成可阅读网页，请继续制作或重试。"
        elif "You've hit your usage limit" in log_text:
            job["status"] = "paused"
            job["message"] = "当前使用额度已达到上限，请稍后继续制作。"
        elif return_code == 124:
            if output:
                job["status"] = "completed"
                job["output"] = str(output)
                job["message"] = "已在时间上限内保存当前深度专题版本。"
            else:
                job["status"] = "failed"
                job["message"] = "在时间上限内未能生成成品，请重试。"
        else:
            job["status"] = "failed"
        job["return_code"] = return_code
        job["finished_at"] = now()
        if job["status"] == "completed":
            if output:
                job["output"] = str(output)
        job.pop("codex_pid", None)
        job.pop("worker_pid", None)
        save_job(job)
    except Exception as exc:  # noqa: BLE001
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n[{now()}] 任务启动失败：{exc}\n")
        job = load_job(job_id) or job
        job["status"] = "failed"
        job["error"] = str(exc)
        job["finished_at"] = now()
        job.pop("codex_pid", None)
        job.pop("worker_pid", None)
        save_job(job)


def pid_alive(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def launch_job(job_id: str) -> None:
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--run-job", job_id],
        cwd=APP_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def recover_jobs() -> None:
    for job in list_jobs():
        if job.get("status") not in {"queued", "running"}:
            continue
        if pid_alive(job.get("worker_pid")):
            continue
        job["status"] = "queued"
        job["recovered_at"] = now()
        job.pop("pid", None)
        job.pop("codex_pid", None)
        job.pop("worker_pid", None)
        save_job(job)
        launch_job(job["id"])


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def log_message(self, format_string: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def authorized(self, path: str) -> bool:
        if not AUTH_PASSWORD or path == "/api/health":
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode("utf-8")
        except Exception:  # noqa: BLE001
            return False
        username, _, password = decoded.partition(":")
        return username == AUTH_USER and password == AUTH_PASSWORD

    def request_auth(self) -> None:
        body = "需要登录后访问播客研究所。".encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Podcast Institute"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_INPUT + 10_000:
            raise ValueError("请求内容为空或过长")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if not self.authorized(path):
            self.request_auth()
            return
        if path in {"/library", "/library/"}:
            body = render_library_index()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/health":
            self.send_json(
                {
                    "ok": True,
                    "root": str(ROOT),
                    "library": str(LIBRARY),
                    "quick_engine": QUICK_ENGINE,
                    "comparison_engine": COMPARISON_ENGINE,
                    "demo_mode": DEMO_MODE,
                }
            )
            return
        if path == "/api/jobs":
            self.send_json([public_job(job) for job in list_jobs()])
            return
        if path == "/api/asr/status":
            self.send_json(asr_status())
            return
        clipboard_match = re.fullmatch(r"/api/clipboard/status/([a-f0-9]{12})", path)
        if clipboard_match:
            session = clipboard_status(clipboard_match.group(1))
            if not session:
                self.send_json({"error": "监听会话不存在"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json(session)
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})", path)
        if match:
            job = load_job(match.group(1))
            if not job:
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            _, log_path = job_paths(job["id"])
            log = ""
            if log_path.exists():
                log = log_path.read_text(encoding="utf-8", errors="replace")[-30_000:]
            self.send_json({**public_job(job), "log": log})
            return
        episodes_match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/episodes", path)
        if episodes_match:
            job = load_job(episodes_match.group(1))
            if not job:
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"episodes": selected_episodes(job)})
            return
        library_match = re.fullmatch(r"/library/(.+\.html)", path)
        if library_match:
            filename = Path(unquote(library_match.group(1))).name
            target = LIBRARY / filename
            if not target.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self.authorized(path):
            self.request_auth()
            return
        if path == "/api/jobs":
            try:
                data = self.read_json()
                mode = data.get("mode")
                request = str(data.get("request", "")).strip()
                episode_count = int(data.get("episode_count", 5))
                recency = str(data.get("recency", "180"))
                production_mode = str(data.get("production_mode", "quick"))
                if mode not in {"topic", "links", "transcript"}:
                    raise ValueError("请选择主题、链接或文稿模式")
                if DEMO_MODE and mode != "links":
                    raise ValueError("公开演示版暂时只开放“小宇宙链接生成速读专题”。")
                if not request or len(request) > MAX_INPUT:
                    raise ValueError("请输入主题、小宇宙链接或文稿")
                if mode == "links" and "xiaoyuzhoufm.com/episode/" not in request:
                    raise ValueError("链接模式需要至少一个小宇宙单集链接")
                if mode == "transcript" and len(request) < 80:
                    raise ValueError("文稿内容太短，请粘贴小宇宙文稿正文")
                if not 3 <= episode_count <= 8:
                    raise ValueError("专题单集数量应在 3–8 期之间")
                if recency not in {"10", "30", "90", "180", "any"}:
                    raise ValueError("时间范围无效")
                if mode == "transcript":
                    production_mode = "quick"
                if DEMO_MODE:
                    production_mode = "quick"
                if production_mode not in {"quick", "deep"}:
                    raise ValueError("生成方式无效")
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return

            job = {
                "id": uuid.uuid4().hex[:12],
                "mode": mode,
                "request": request,
                "episode_count": episode_count,
                "recency": recency,
                "production_mode": production_mode,
                "status": "queued",
                "created_at": now(),
            }
            if mode == "transcript":
                cached = cache_user_paste_if_possible(request)
                if cached:
                    job["cached_episode_id"] = cached["episode_id"]
            save_job(job)
            launch_job(job["id"])
            self.send_json(job, HTTPStatus.CREATED)
            return

        if path == "/api/open-library":
            self.send_json({"ok": True, "url": "/library"})
            return

        if path == "/api/clipboard/watch":
            if DEMO_MODE:
                self.send_json({"error": "公开演示版暂不开放剪贴板导入。"}, HTTPStatus.FORBIDDEN)
                return
            if sys.platform != "darwin":
                self.send_json(
                    {"error": "当前系统暂不支持剪贴板自动捕获，请手动粘贴。"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            session_id = uuid.uuid4().hex[:12]
            with CLIPBOARD_LOCK:
                CLIPBOARD_SESSIONS[session_id] = {
                    "id": session_id,
                    "status": "listening",
                    "started_at": now(),
                    "message": "正在监听剪贴板，请在小宇宙 App 中复制文稿。",
                }
            threading.Thread(
                target=watch_clipboard,
                args=(session_id,),
                daemon=True,
            ).start()
            self.send_json(CLIPBOARD_SESSIONS[session_id], HTTPStatus.CREATED)
            return

        if path == "/api/asr/configure":
            if DEMO_MODE:
                self.send_json({"ok": False, "message": "公开演示版不开放云端 ASR 配置。"}, HTTPStatus.FORBIDDEN)
                return
            try:
                data = self.read_json()
                provider = str(data.get("provider", "tencent"))
                if provider == "groq":
                    if not save_groq_api_key or not test_groq_credentials:
                        raise ValueError("Groq ASR 模块不可用")
                    save_groq_api_key(str(data.get("api_key", "")))
                    result = test_groq_credentials()
                else:
                    save_tencent_credentials(
                        str(data.get("secret_id", "")),
                        str(data.get("secret_key", "")),
                    )
                    result = test_tencent_credentials()
                self.send_json(result, HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_REQUEST)
            except (ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
                self.send_json({"ok": False, "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/asr/test":
            if DEMO_MODE:
                self.send_json({"ok": False, "message": "公开演示版不开放云端 ASR 测试。"}, HTTPStatus.FORBIDDEN)
                return
            result = test_credentials()
            self.send_json(result, HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_REQUEST)
            return

        retry_match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/retry", path)
        if retry_match:
            job = load_job(retry_match.group(1))
            if not job:
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            if job.get("status") not in {"paused", "failed"}:
                self.send_json({"error": "当前任务不需要重试"}, HTTPStatus.BAD_REQUEST)
                return
            job["status"] = "queued"
            job["retried_at"] = now()
            job.pop("message", None)
            job.pop("error", None)
            job.pop("return_code", None)
            save_job(job)
            launch_job(job["id"])
            self.send_json(job)
            return

        deepen_match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/deepen", path)
        if deepen_match:
            if DEMO_MODE:
                self.send_json({"error": "公开演示版暂不开放精读升级。"}, HTTPStatus.FORBIDDEN)
                return
            source = load_job(deepen_match.group(1))
            if not source:
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            if source.get("status") != "completed":
                self.send_json({"error": "请等待速读专题完成后再升级"}, HTTPStatus.BAD_REQUEST)
                return
            if source.get("production_mode", "quick") != "quick":
                self.send_json({"error": "当前任务已经是深度专题"}, HTTPStatus.BAD_REQUEST)
                return
            # 可选 body：{"episode": "<episode_id 或链接片段>"} → 只补这一期全文（避开限流）。
            payload = {}
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, json.JSONDecodeError):
                payload = {}
            target_episode = (payload.get("episode") or "").strip()
            job = {
                "id": uuid.uuid4().hex[:12],
                "mode": source["mode"],
                "request": source["request"],
                "episode_count": source["episode_count"],
                "recency": source["recency"],
                # 指定 episode 时走单期补全文（deep_one），否则沿用全量精读（deep）。
                "production_mode": "deep_one" if target_episode else "deep",
                "status": "queued",
                "created_at": now(),
                "parent_job_id": source["id"],
            }
            if target_episode:
                job["target_episode"] = target_episode
            save_job(job)
            launch_job(job["id"])
            self.send_json(job, HTTPStatus.CREATED)
            return

        rerun_match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/rerun", path)
        if rerun_match:
            source = load_job(rerun_match.group(1))
            if not source:
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            job = {
                "id": uuid.uuid4().hex[:12],
                "mode": source["mode"],
                "request": source["request"],
                "episode_count": source["episode_count"],
                "recency": source["recency"],
                "production_mode": source.get("production_mode", "quick"),
                "status": "queued",
                "created_at": now(),
                "rerun_from_job_id": source["id"],
            }
            save_job(job)
            launch_job(job["id"])
            self.send_json(job, HTTPStatus.CREATED)
            return

        self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--run-job":
        run_job(sys.argv[2])
        return
    if not CODEX.exists():
        print(f"提示：找不到 Codex 可执行文件：{CODEX}。网页和书架仍会启动，生成任务可能不可用。")
    recover_jobs()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"播客专题研究助手已启动：{url}")
    if os.environ.get("PODCAST_ASSISTANT_NO_BROWSER") != "1":
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
