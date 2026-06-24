#!/usr/bin/env python3
"""Fetch, transcribe, and package Xiaoyuzhou episodes as illustrated HTML or EPUB."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import mimetypes
import re
import shutil
import sys
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

USER_AGENT = "Mozilla/5.0 AppleWebKit/537.36 Chrome/126 Safari/537.36"
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo-q4"
GLOBAL_TRANSCRIPT_CACHE = (
    Path(
        __import__("os").environ.get(
            "PODCAST_TRANSCRIPT_CACHE",
            "~/Library/Caches/xiaoyuzhou-illustrated-ebook/transcript-cache",
        )
    ).expanduser()
)
ASSISTANT_DIR = Path(__file__).resolve().parents[3]


@dataclass
class Episode:
    eid: str
    url: str
    title: str
    podcast: str
    description: str
    shownotes_html: str
    audio_url: str
    mime_type: str
    audio_size: int
    duration: int
    pub_date: str


def request(url: str):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=90
    )


def read_urls(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def parse_episode(url: str) -> Episode:
    with request(url) as response:
        page = response.read().decode("utf-8")
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        page,
        re.DOTALL,
    )
    if not match:
        raise RuntimeError("页面中没有找到公开单集数据。")
    raw = json.loads(html.unescape(match.group(1)))["props"]["pageProps"]["episode"]
    media = raw.get("media") or {}
    audio_url = (raw.get("enclosure") or {}).get("url") or (
        media.get("source") or {}
    ).get("url")
    if not audio_url:
        raise RuntimeError("单集没有公开音频，可能是付费或私密内容。")
    podcast = raw.get("podcast") or {}
    return Episode(
        eid=raw["eid"],
        url=url,
        title=text(raw.get("title")),
        podcast=text(podcast.get("title")),
        description=text(raw.get("description")),
        shownotes_html=text(raw.get("shownotes")),
        audio_url=audio_url,
        mime_type=text(media.get("mimeType")),
        audio_size=int(media.get("size") or 0),
        duration=int(raw.get("duration") or 0),
        pub_date=text(raw.get("pubDate")),
    )


def audio_suffix(episode: Episode) -> str:
    suffix = Path(episode.audio_url.split("?", 1)[0]).suffix.lower()
    if suffix in {".mp3", ".m4a", ".mp4", ".wav"}:
        return suffix
    return ".m4a" if episode.mime_type == "audio/mp4" else ".mp3"


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    with request(url) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output, 1024 * 1024)
    temporary.replace(target)


def fetch(episodes_file: Path, cache: Path) -> None:
    urls = read_urls(episodes_file)
    if not urls:
        raise RuntimeError("episodes.txt 中没有链接。")
    ids, failures = [], []
    for index, url in enumerate(urls, 1):
        try:
            episode = parse_episode(url)
            ids.append(episode.eid)
            folder = cache / "episodes" / episode.eid
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "metadata.json").write_text(
                json.dumps(asdict(episode), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            audio = folder / f"audio{audio_suffix(episode)}"
            if not audio.exists():
                print(f"[{index}/{len(urls)}] 下载：{episode.title}")
                download(episode.audio_url, audio)
            else:
                print(f"[{index}/{len(urls)}] 已缓存：{episode.title}")
            restore_global_transcript(episode, folder)
        except Exception as exc:
            failures.append({"url": url, "error": str(exc)})
            print(f"[{index}/{len(urls)}] 失败：{url}：{exc}", file=sys.stderr)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "collection.json").write_text(
        json.dumps({"episode_ids": ids, "failures": failures}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_episodes(cache: Path) -> list[tuple[Episode, Path]]:
    manifest = json.loads((cache / "collection.json").read_text(encoding="utf-8"))
    result = []
    for eid in manifest.get("episode_ids", []):
        folder = cache / "episodes" / eid
        result.append(
            (
                Episode(**json.loads((folder / "metadata.json").read_text(encoding="utf-8"))),
                folder,
            )
        )
    return result


def find_audio(folder: Path) -> Path:
    matches = [path for path in folder.glob("audio.*") if not path.name.endswith(".part")]
    if not matches:
        raise RuntimeError(f"{folder} 没有音频。")
    return matches[0]


def transcript_cache_dir(eid: str) -> Path:
    return GLOBAL_TRANSCRIPT_CACHE / safe_filename(eid)


def restore_global_transcript(episode: Episode, folder: Path) -> bool:
    """Copy a previously created transcript into this project cache if available."""
    target = folder / "transcript.json"
    if target.exists():
        return True
    source = transcript_cache_dir(episode.eid) / "transcript.json"
    if not source.exists():
        return False
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    manifest = transcript_cache_dir(episode.eid) / "manifest.json"
    if manifest.exists():
        shutil.copy2(manifest, folder / "transcript_source.json")
    print(f"复用全局转写缓存：{episode.title}")
    return True


def store_global_transcript(episode: Episode, folder: Path, source: str) -> None:
    """Persist a transcript so future projects never transcribe the same episode twice."""
    transcript = folder / "transcript.json"
    if not transcript.exists():
        return
    target_dir = transcript_cache_dir(episode.eid)
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(transcript, target_dir / "transcript.json")
    manifest = {
        "episode_id": episode.eid,
        "title": episode.title,
        "podcast": episode.podcast,
        "url": episode.url,
        "duration": episode.duration,
        "source": source,
        "cached_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def transcribe(cache: Path, model: str) -> None:
    import mlx_whisper

    episodes = load_episodes(cache)
    for index, (episode, folder) in enumerate(episodes, 1):
        target = folder / "transcript.json"
        if restore_global_transcript(episode, folder):
            print(f"[{index}/{len(episodes)}] 已转写：{episode.title}")
            continue
        print(f"[{index}/{len(episodes)}] 转写：{episode.title}")
        result = mlx_whisper.transcribe(
            str(find_audio(folder)),
            path_or_hf_repo=model,
            language="zh",
            verbose=False,
            condition_on_previous_text=True,
        )
        target.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        store_global_transcript(episode, folder, "local-whisper")


def ensure_assistant_import_path() -> None:
    if str(ASSISTANT_DIR) not in sys.path:
        sys.path.insert(0, str(ASSISTANT_DIR))


def prepare_cloud_items(episodes_file: Path, cache: Path, provider_name: str) -> tuple[list[dict], dict[str, Episode]]:
    urls = read_urls(episodes_file)
    if not urls:
        raise RuntimeError("episodes.txt 中没有链接。")
    items: list[dict] = []
    episode_by_id: dict[str, Episode] = {}
    for index, url in enumerate(urls, 1):
        episode = parse_episode(url)
        episode_by_id[episode.eid] = episode
        folder = cache / "episodes" / episode.eid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "metadata.json").write_text(
            json.dumps(asdict(episode), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if restore_global_transcript(episode, folder):
            print(f"[{index}/{len(urls)}] 已有全文缓存：{episode.title}")
            continue
        item = {
            "eid": episode.eid,
            "title": episode.title,
            "audio_url": episode.audio_url,
        }
        try:
            item["audio_path"] = str(find_audio(folder))
        except RuntimeError:
            pass
        items.append(item)
        print(f"[{index}/{len(urls)}] 已准备 {provider_name} 转写：{episode.title}")
    return items, episode_by_id


def store_cloud_results(items: list[dict], episode_by_id: dict[str, Episode], cache: Path, source: str) -> None:
    for item in items:
        episode = episode_by_id.get(item["eid"])
        if episode:
            store_global_transcript(episode, cache / "episodes" / episode.eid, source)


def transcribe_cloud(episodes_file: Path, cache: Path) -> None:
    ensure_assistant_import_path()
    from tencent_asr import transcribe_many

    items, episode_by_id = prepare_cloud_items(episodes_file, cache, "腾讯云")
    if not items:
        print("全部单集已有全局转写缓存，无需提交腾讯云 ASR。")
        return
    print(f"并行提交 {len(items)} 期音频到腾讯云 ASR…")
    results = transcribe_many(items, cache / "episodes", workers=min(5, len(items)))
    store_cloud_results(items, episode_by_id, cache, "tencent-cloud-asr")
    print(f"云端全文转写完成：{len(results)}/{len(items)}")


def transcribe_groq(episodes_file: Path, cache: Path, model: str = "whisper-large-v3-turbo") -> None:
    ensure_assistant_import_path()
    from groq_asr import transcribe_many

    items, episode_by_id = prepare_cloud_items(episodes_file, cache, "Groq")
    if not items:
        print("全部单集已有全局转写缓存，无需提交 Groq ASR。")
        return
    print(f"并行提交 {len(items)} 期音频到 Groq ASR（{model}）…")
    results = transcribe_many(
        items, cache / "episodes", workers=min(3, len(items)), model=model
    )
    store_cloud_results(items, episode_by_id, cache, f"groq-asr:{model}")
    print(f"Groq 全文转写完成：{len(results)}/{len(items)}")


def transcribe_auto(episodes_file: Path, cache: Path) -> None:
    ensure_assistant_import_path()
    try:
        from groq_asr import credentials_status as groq_status

        if groq_status().get("configured"):
            print("检测到 Groq API Key，优先使用 Groq 低成本快速转写。")
            transcribe_groq(episodes_file, cache)
            return
    except Exception as exc:  # noqa: BLE001
        print(f"Groq ASR 不可用，准备尝试腾讯云：{exc}", file=sys.stderr)

    try:
        from tencent_asr import credentials_status as tencent_status

        if tencent_status().get("configured"):
            print("未检测到 Groq API Key，使用腾讯云 ASR。")
            transcribe_cloud(episodes_file, cache)
            return
    except Exception as exc:  # noqa: BLE001
        print(f"腾讯云 ASR 不可用：{exc}", file=sys.stderr)

    raise RuntimeError(
        "没有可用的云端 ASR。请配置 Groq API Key 或腾讯云 ASR；不想花钱时请使用快速专题/文稿模式/本地 Whisper。"
    )


def markdown_html(markdown_text: str) -> str:
    import markdown

    return markdown.markdown(markdown_text, extensions=["extra", "sane_lists"])


def chapter_title(markdown_text: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", markdown_text, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def ordered_markdown_files(project: Path) -> list[Path]:
    """Return visible chapters. Cross-episode comparison is a front-matter chapter."""
    article_files = sorted((project / "文章").glob("*.md"))
    if not article_files:
        raise RuntimeError("项目中没有 文章/*.md。")
    front_matter = [
        path
        for path in [project / "跨集比对.md", project / "导读.md"]
        if path.exists()
    ]
    return front_matter + article_files


def media_type(path: Path) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    return guessed or "application/octet-stream"


def inline_images(markup: str, article_path: Path) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    for image in soup.find_all("img"):
        raw_path = image.get("src", "").strip()
        if not raw_path or raw_path.startswith(("http://", "https://", "data:")):
            continue
        source = (article_path.parent / raw_path).resolve()
        if not source.exists():
            raise FileNotFoundError(f"插图不存在：{source}")
        encoded = base64.b64encode(source.read_bytes()).decode("ascii")
        image["src"] = f"data:{media_type(source)};base64,{encoded}"
    return str(soup)


def compact_evidence_markers(markup: str) -> str:
    """Make relationship and source markers quieter in the generated HTML."""
    replacements = {
        "✅共识": '<span class="relation-badge relation-consensus">共识</span>',
        "⚠️分歧": '<span class="relation-badge relation-diff">分歧</span>',
        "◐部分分歧": '<span class="relation-badge relation-partial">部分分歧</span>',
        "💡独有": '<span class="relation-badge relation-unique">独有</span>',
        "❓信息冲突": '<span class="relation-badge relation-conflict">信息冲突</span>',
        "🎧 听稿依据": "听稿",
        "📝 简介依据": "简介",
        "🌐 联网核验": "核验",
        "🤔 AI 推断": "推断",
    }
    for source, target in replacements.items():
        markup = markup.replace(source, target)
    return markup

def strip_frontend_technical_notes(markup: str) -> str:
    """Remove backend-oriented caveats from the visible reading surface."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    technical_patterns = (
        "transcript.json",
        "0 期听稿依据",
        "0期听稿依据",
        "未命中本地",
        "以下内容不假装已经听完整期",
        "也不把节目简介改写成主播原话",
    )
    for tag in list(soup.find_all(["p", "li"])):
        text_value = tag.get_text(" ", strip=True)
        if any(pattern in text_value for pattern in technical_patterns):
            tag.decompose()
    return str(soup)


def strip_markup(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def split_markdown_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def source_marker(value: str) -> tuple[str, str]:
    if "source-audio" in value or "听稿依据" in value:
        return "source-audio", "听稿"
    if "source-web" in value or "联网核验" in value:
        return "source-web", "核验"
    if "source-infer" in value or "AI 推断" in value:
        return "source-infer", "推断"
    return "source-intro", "简介"


def clean_claim_text(value: str) -> str:
    text_value = strip_markup(value)
    text_value = re.sub(r"^(✅共识|⚠️分歧|◐部分分歧|💡独有|❓信息冲突)\s*", "", text_value)
    text_value = re.sub(
        r"^[🎧📝🌐🤔]?\s*(听稿依据|简介依据|联网核验|AI 推断|听稿|简介|核验|推断)\s*",
        "",
        text_value,
    )
    text_value = re.sub(r"\s+", " ", text_value).strip()
    text_value = re.sub(r"^[：:，,。；;\s]+", "", text_value)
    return text_value


def split_branch_title(header: str) -> tuple[str, str]:
    text_value = strip_markup(header)
    for separator in ("：", ":"):
        if separator in text_value:
            left, right = text_value.split(separator, 1)
            return left.strip(), right.strip()
    return text_value, "这一路径的核心回答"


def truncate_text(value: str, limit: int = 54) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip("，。；、 ") + "…"


def complete_short_text(value: str, limit: int = 72) -> str:
    """Return a compact complete sentence, avoiding visible ellipsis."""
    text = re.sub(r"\s+", " ", strip_markup(value)).strip()
    text = re.sub(r"^(跨集比对|导读)\s*[：: ]*", "", text)
    text = re.sub(r"^[#\s]+", "", text)
    text = re.sub(r"^[：:，,。；;\s]+", "", text)
    if not text:
        return "本节整理一个关键判断。"
    for mark in "。！？":
        position = text.find(mark)
        if 8 <= position + 1 <= limit:
            return text[: position + 1]
    cut = max(
        text.rfind("，", 0, limit),
        text.rfind("；", 0, limit),
        text.rfind("、", 0, limit),
        text.rfind("：", 0, limit),
    )
    if cut >= 12:
        return text[:cut].rstrip("，；、： ") + "。"
    return text[:limit].rstrip("，；、： ") + "。"


def build_viewpoint_map(project: Path, title: str) -> str:
    comparison = project / "跨集比对.md"
    if not comparison.exists():
        return ""
    raw = comparison.read_text(encoding="utf-8")
    lines = raw.splitlines()
    table_start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("|") and "子议题" in line:
            table_start = index
            break
    if table_start is None or table_start + 2 >= len(lines):
        return ""
    headers = split_markdown_row(lines[table_start])
    rows = []
    for line in lines[table_start + 2 :]:
        if not line.strip().startswith("|"):
            break
        cells = split_markdown_row(line)
        if len(cells) == len(headers):
            rows.append(cells)
    if len(headers) < 3 or not rows:
        return ""

    branches = []
    for row in rows[:5]:
        topic = strip_markup(row[0]) or "回答路径"
        if "存疑" in topic or "需要继续" in topic:
            continue
        sources = []
        relation = "◐"
        for index, cell in enumerate(row[1:], 1):
            cleaned = clean_claim_text(cell)
            if not cleaned or cleaned == "—未提及" or "未提及" in cleaned:
                continue
            if cell.startswith("✅") or "✅共识" in cell:
                relation = "✅"
            elif cell.startswith("⚠️") or "分歧" in cell:
                relation = "⚠️"
            elif cell.startswith("💡") or "独有" in cell:
                relation = "💡"
            elif cell.startswith("❓") or "冲突" in cell:
                relation = "❓"
            podcast, angle = split_branch_title(headers[index])
            source_class, source_label = source_marker(cell)
            sources.append(
                {
                    "podcast": podcast,
                    "angle": angle,
                    "summary": complete_short_text(cleaned, 42),
                    "source_class": source_class,
                    "source_label": source_label,
                }
            )
        if not sources:
            continue
        branches.append(
            {
                "topic": truncate_text(topic, 16),
                "relation": relation,
                "sources": sources[:3],
            }
        )
    if not branches:
        return ""

    summary = "把入选节目放到同一张图里，看它们如何回答这个问题。"
    center_title = re.sub(r"^播客速读[：:]?", "", title).strip() or "中心问题"
    folder_labels = ["最重要线索", "主要分歧", "独有观察"]
    folders = []
    cards = []
    for index, branch in enumerate(branches[:4], 1):
        source = branch["sources"][0]
        if index <= 3:
            folders.append(
                f"""<div class="folder-card folder-{index}">
<div class="folder-tab"></div>
<div class="folder-label">{html.escape(folder_labels[index - 1])}</div>
<p>{html.escape(branch['sources'][0]['summary'])}</p>
</div>"""
            )
        cards.append(
            f"""<button class="cork-note note-{index}" type="button">
<span class="pin"></span>
<div class="clean-map-index">{index:02d}</div>
<h3>{html.escape(branch['topic'])}</h3>
<p>{html.escape(branch['sources'][0]['summary'])}</p>
<div class="clean-map-source {source['source_class']}"><b>{html.escape(source['source_label'])}</b>{html.escape(source['podcast'])}</div>
</button>"""
        )
    return f"""<section class="focus-folders" id="focus-folders" aria-label="三个重点">
<div class="section-head"><span>①</span><div><h2>三个重点</h2><p>先抓住这份档案里最值得带走的判断。</p></div></div>
<div class="folder-strip">{''.join(folders)}</div>
</section>
<section class="view-map" id="viewpoint-map" aria-label="观点地图">
<div class="section-head"><span>②</span><div><h2>观点地图</h2><p>{html.escape(summary)}</p></div></div>
<figure class="clean-map">
<div class="clean-map-board cork-board">
<div class="thread-line line-a"></div><div class="thread-line line-b"></div><div class="thread-line line-c"></div>
<div class="clean-map-center"><span>中心问题</span><strong>{html.escape(truncate_text(center_title, 24))}</strong></div>
<div class="clean-map-grid cork-grid">{''.join(cards)}</div>
</div>
<figcaption>每张便利贴对应一条回答路径；更完整的依据在下方展开。</figcaption>
</figure>
</section>"""

def build_quick_cards_from_comparison(project: Path) -> str:
    comparison = project / "跨集比对.md"
    if not comparison.exists():
        return ""
    raw = comparison.read_text(encoding="utf-8")
    lines = raw.splitlines()
    table_start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("|") and "子议题" in line:
            table_start = index
            break
    if table_start is None or table_start + 2 >= len(lines):
        return ""
    headers = split_markdown_row(lines[table_start])
    cards = []
    for row_index, line in enumerate(lines[table_start + 2 :], 1):
        if not line.strip().startswith("|") or len(cards) >= 6:
            break
        cells = split_markdown_row(line)
        if len(cells) != len(headers):
            continue
        title_text = strip_markup(cells[0])
        if not title_text or "存疑" in title_text:
            continue
        candidates = []
        for cell in cells[1:]:
            cleaned = clean_claim_text(cell)
            if cleaned and "未提及" not in cleaned:
                candidates.append(cleaned)
        if not candidates:
            continue
        summary = complete_short_text(candidates[0], 74)
        cards.append(
            f"""<article class="quick-card-lite">
<div class="quick-number">{len(cards)+1:02d}</div>
<h3>{html.escape(title_text)}</h3>
<p>{html.escape(summary)}</p>
</article>"""
        )
    if not cards:
        return ""
    return f"""<section class="quick-read" id="quick-read" aria-label="速读卡片">
<div class="section-head"><span>③</span><div><h2>三分钟读完</h2><p>先看这几张卡，抓住本页真正要回答的问题。</p></div></div>
<div class="quick-grid">{''.join(cards)}</div>
</section>"""

def extract_markdown_section(raw: str, heading: str) -> str:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.M)
    match = pattern.search(raw)
    if not match:
        return ""
    start = match.end()
    next_match = re.search(r"^##\s+", raw[start:], re.M)
    end = start + next_match.start() if next_match else len(raw)
    return raw[start:end].strip()


def build_comparison_sections(project: Path) -> tuple[str, str]:
    comparison = project / "跨集比对.md"
    if not comparison.exists():
        return "", ""
    raw = comparison.read_text(encoding="utf-8")
    matrix_parts = []
    matrix_map = [
        ("大家争的是什么", "分歧"),
        ("没争议的共识", "共识"),
        ("只有某一期提到的独到观点", "独有观点"),
    ]
    for heading, label in matrix_map:
        section = extract_markdown_section(raw, heading)
        if section:
            matrix_parts.append(
                f"""<div class="matrix-block">
<div class="matrix-label">{html.escape(label)}</div>
<h3>{html.escape(heading)}</h3>
<div>{compact_evidence_markers(markdown_html(section))}</div>
</div>"""
            )
    matrix_html = ""
    if matrix_parts:
        matrix_html = f"""<section class="matrix-section" id="matrix-section" aria-label="共识分歧与独有观点">
<div class="section-head"><span>④</span><div><h2>共识、分歧和独有观点</h2><p>把多期节目放在一起看，区分哪些是共同判断，哪些只是单期视角。</p></div></div>
<div class="matrix-stack">{''.join(matrix_parts)}</div>
</section>"""

    overview = extract_markdown_section(raw, "各期速览")
    episode_items = []
    if overview:
        for line in overview.splitlines():
            line = line.strip()
            if not re.match(r"^\d+\.", line):
                continue
            line = re.sub(r"^\d+\.\s*", "", line)
            line = clean_claim_text(line)
            line = re.sub(r"《([^》]+)》[：:]\s*", r"\1：", line)
            episode_items.append(f"<li>{html.escape(complete_short_text(line, 78))}</li>")
    sources_html = ""
    if episode_items:
        sources_html = f"""<section class="sources-section" id="sources-section" aria-label="来源与依据">
<div class="section-head"><span>⑤</span><div><h2>来源与依据</h2><p>本页主要依据入选节目的公开介绍和可访问资料整理。对外引用前，建议回到原节目核对。</p></div></div>
<div class="source-card source-card-wide"><h3>这次参考了这些节目</h3><ol>{''.join(episode_items)}</ol></div>
</section>"""
    return matrix_html, sources_html

def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value).strip(" .")
    return cleaned or "小宇宙图文阅读"


def default_web_library(project: Path) -> Path:
    resolved = project.resolve()
    for folder in (resolved, *resolved.parents):
        if folder.name == "播客收藏":
            return folder / "网页书架"
    return resolved / "网页书架"


def write_library_index(library: Path) -> None:
    # The local app now owns the interactive bookshelf at /library.  Keep this
    # file as a stable fallback only, so future build-html runs do not overwrite
    # the designed bookshelf with the old static beige list.
    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="0; url=http://127.0.0.1:8765/library">
<title>打开播客研究所书架</title>
<style>
body{background:#101116;color:#eee8d9;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;margin:0;display:grid;min-height:100vh;place-items:center}
a{color:#ff6a2a;font-weight:800}
</style></head><body>
<main><p>正在打开播客研究所书架……</p><p><a href="http://127.0.0.1:8765/library">如果没有自动跳转，点这里打开</a></p></main>
</body></html>"""
    (library / "index.html").write_text(page, encoding="utf-8")


def build_html(project: Path, title: str, output: Path | None) -> Path:
    ordered = ordered_markdown_files(project)
    sections, toc = [], []
    viewpoint_map = build_viewpoint_map(project, title)
    matrix_html, sources_html = build_comparison_sections(project)
    if viewpoint_map:
        toc.append('<li><a href="#focus-folders">三个重点</a></li>')
        toc.append('<li><a href="#viewpoint-map">观点地图</a></li>')
    for index, path in enumerate(ordered, 1):
        raw = path.read_text(encoding="utf-8")
        heading = chapter_title(raw, path.stem)
        chapter_id = f"chapter-{index}"
        body = strip_frontend_technical_notes(
            compact_evidence_markers(inline_images(markdown_html(raw), path))
        )
        # 速读版首屏只保留观点地图；其余章节默认折叠（渐进披露）。
        # 跨集比对（含观点矩阵）与正文长文用不同的折叠提示。
        stem = path.stem
        if stem == "跨集比对":
            summary_label = "查看完整观点矩阵"
        elif stem == "导读":
            summary_label = "展开导读"
        else:
            summary_label = "阅读完整文章"
        sections.append(
            f'<details class="chapter-fold" id="fold-{index}">'
            f'<summary class="chapter-summary"><span class="fold-label">{summary_label}</span>'
            f'<span class="fold-title">{html.escape(heading)}</span></summary>'
            f'<article id="{chapter_id}" data-title="{html.escape(heading)}">'
            f"{body}</article></details>"
        )

    chapter_count = len(sections)
    # 观点地图已经承担了“快速抓结论”的职责。单独再放一组速读卡会
    # 与便利贴内容重复，让用户误以为两者有不同含义，因此生成页默认不再展示。
    quick_read_html = ""
    if matrix_html:
        toc.append('<li><a href="#matrix-section">共识、分歧和独有观点</a></li>')
    if sources_html:
        toc.append('<li><a href="#sources-section">来源与依据</a></li>')
    toc.append('<li><a href="#article-drawers">完整分析</a></li>')
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>{html.escape(title)}</title>
<style>
:root{{--dark:#101116;--panel:#20212b;--panel2:#171822;--line-dark:#343548;--crt:#050708;--orange:#ff5c1a;--paper:#eee8d9;--paper-soft:#f8f1df;--ink:#181824;--muted:#777063;--rule:#c8bda8;--accent:#b7552d}}
*{{box-sizing:border-box}} html{{scroll-behavior:smooth}} body{{background:radial-gradient(circle at 50% -10%,#343540 0,#171821 34%,#090a0e 72%,#050506 100%);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans SC",sans-serif;line-height:1.9;margin:0}}
.machine-cover{{display:grid;grid-template-columns:minmax(0,760px) 130px;gap:20px;justify-content:center;padding:18px 20px 16px}} .machine-frame{{background:linear-gradient(180deg,#2a2a38,#1e1e2a 55%,#191922);border:1px solid #3a3a4e;border-radius:3px;box-shadow:0 10px 60px #0009,inset 0 1px 0 #ffffff12;grid-column:1;max-width:760px;position:relative;width:100%}}
.machine-frame::before,.machine-frame::after{{align-items:center;background:radial-gradient(circle,#323242 40%,#242432);border:1px solid #424254;border-radius:50%;color:#555568;content:"+";display:flex;font:400 14px "Courier New",monospace;height:16px;justify-content:center;position:absolute;top:14px;width:16px;z-index:3}} .machine-frame::before{{left:12px}} .machine-frame::after{{right:12px}}
.machine-top{{align-items:center;border-bottom:1px solid #242432;display:flex;justify-content:space-between;padding:10px 28px}} .machine-brand{{align-items:center;color:#5e5e78;display:flex;font:900 10px "Courier New",monospace;gap:10px;letter-spacing:.22em;text-transform:uppercase}} .machine-pi{{align-items:center;background:#ff5c1a;border-radius:2px;box-shadow:0 0 10px #ff5c1a80;color:#13131c;display:flex;font:900 7px "Courier New",monospace;height:20px;justify-content:center;letter-spacing:0;width:20px}}
.machine-leds{{display:flex;gap:16px}} .machine-leds span{{color:#464660;font:900 8px "Courier New",monospace;letter-spacing:.15em}} .machine-leds span::before{{background:#2e2e40;border-radius:50%;content:"";display:inline-block;height:8px;margin-right:6px;vertical-align:-1px;width:8px}} .machine-leds span:first-child::before,.machine-leds span:nth-child(2)::before,.machine-leds span:nth-child(3)::before,.machine-leds span:last-child::before{{background:#ff5c1a;box-shadow:0 0 10px #ff5c1a70}}
.machine-body{{padding:16px 24px 18px}} .machine-screen{{background:#040e1c;border:2px solid #0c1626;border-radius:2px;box-shadow:inset 0 0 50px #000d;min-height:188px;overflow:hidden;padding:26px 24px;position:relative}} .machine-screen::before{{background:repeating-linear-gradient(0deg,transparent 0 2px,#0003 2px 3px),radial-gradient(ellipse at center,transparent 35%,#0009 100%);content:"";inset:0;pointer-events:none;position:absolute;z-index:3}}
.machine-kicker{{color:#ff5c1a;font:900 10px "Courier New",monospace;letter-spacing:.28em;margin-bottom:4px;opacity:.55;position:relative;z-index:4}} .machine-title{{color:#ff9060;font-family:"Songti SC","Noto Serif SC",serif;font-size:24px;letter-spacing:.04em;line-height:1.2;position:relative;text-shadow:0 0 14px #ff5c1aa6;z-index:4}} .machine-title span{{animation:cursorBlink 1.1s linear infinite;color:#ff5c1a;font-size:18px;margin-left:4px}} .machine-sub{{color:#ff5c1a;font:900 10px "Courier New",monospace;letter-spacing:.12em;margin-top:5px;opacity:.38;position:relative;z-index:4}}
.machine-bars{{align-items:end;display:flex;gap:2px;height:44px;margin-top:25px;position:relative;z-index:4}} .machine-bars i{{animation:barPulse 780ms ease-in-out infinite alternate;background:#ff5c1a;box-shadow:0 0 9px #ff5c1a66;min-height:5px;width:3px}} .machine-bars i:nth-child(2n){{animation-duration:540ms}} .machine-bars i:nth-child(3n){{animation-duration:920ms}} .machine-status{{color:#ff5c1a;font:900 9px "Courier New",monospace;letter-spacing:.3em;margin-top:8px;opacity:.3;position:relative;z-index:4}}
.machine-mode-label{{color:#464660;font:900 8px "Courier New",monospace;letter-spacing:.25em;margin:14px 0 8px;text-transform:uppercase}} .machine-controls{{display:grid;gap:8px;grid-template-columns:repeat(3,1fr)}} .machine-controls a{{background:#191924;border:1px solid #2c2c3e;border-radius:2px;box-shadow:inset 0 1px 0 #ffffff0a;color:#686878;padding:10px 12px;text-align:center;text-decoration:none}} .machine-controls a small{{color:#404054;display:block;font:900 8px "Courier New",monospace;letter-spacing:.22em;margin-bottom:3px;text-transform:uppercase}} .machine-controls a:hover{{border-color:#ff5c1a;color:#ffb380}}
.layout{{display:grid;grid-template-columns:minmax(0,760px) 130px;gap:20px;justify-content:center;padding:0 20px 110px}} aside{{background:transparent;border:0;color:#d8d3ca;display:block;grid-column:2;grid-row:1;padding-top:8px}}
.search,.web,.source-legend{{display:none}} input{{background:#101116;border:1px solid #343548;color:#eee8d9;font-size:14px;min-width:0;padding:10px 12px;width:100%}} button{{background:#111116;border:1px solid #343548;color:#f0eadc;cursor:pointer;font-weight:900;padding:10px 12px;white-space:nowrap}}
nav{{display:block;position:sticky;top:24px}} nav::before{{color:#a06a32;content:"本档目录";display:block;font:900 8px "Courier New",monospace;letter-spacing:.2em;margin-bottom:10px}} nav ol{{border-left:1px solid var(--rule);font-size:.74rem;list-style:none;margin:0;padding:0}} nav li{{margin:0}} nav a{{color:#8e8fa2;display:block;font-size:.72rem;line-height:1.4;padding:7px 8px;text-decoration:none}} nav a:hover{{border-left:1px solid #d8442f;color:#f3ecd8;margin-left:-1px}} .toc-tools{{border-top:1px solid #343548;display:grid;gap:6px;margin-top:14px;padding-top:12px}} .toc-tools a,.toc-tools button{{background:transparent;border:1px solid #343548;color:#8e8fa2;font:900 9px "Courier New",monospace;letter-spacing:.06em;padding:6px 8px;text-align:left;text-decoration:none}} .toc-tools a:hover,.toc-tools button:hover{{border-color:#ff5c1a;color:#ffb380}}
main{{background:#f3ecd8;border-left:1px solid #cbbf9e;border-right:1px solid #cbbf9e;border-bottom:1px solid #c4b48a;box-shadow:0 8px 40px #0008,inset 0 1px 0 #ffffff99;grid-column:1;grid-row:1;padding:28px 28px 40px 64px;position:relative}} main::before{{content:"";position:absolute;left:13px;top:0;bottom:0;width:10px;background:radial-gradient(circle,#13131c 0 5px,transparent 5.6px) center 24px/10px 44px repeat-y}} main::after{{content:"";position:absolute;left:37px;top:0;bottom:0;border-left:1px solid #d9cfae;opacity:1}}
.empty{{color:var(--muted);display:none}} .chapter-fold,.view-map,.dossier-cover{{position:relative;z-index:1}}
.dossier-cover{{border-bottom:1px dotted var(--rule);margin:0 0 24px;padding:0 0 24px;position:relative}} .dossier-cover::after{{color:#d8442f;content:"?";font-family:"Songti SC","Noto Serif SC",serif;font-size:72px;font-weight:700;line-height:1;opacity:.1;pointer-events:none;position:absolute;right:4px;top:-8px}} .dossier-no{{color:#a06a32;font:900 .7rem "Courier New",monospace;letter-spacing:.2em;margin-bottom:10px;text-transform:uppercase}} .dossier-cover h1{{font-family:"Songti SC","Noto Serif SC",serif;font-size:clamp(1.85rem,5vw,2.15rem);font-weight:700;line-height:1.25;margin:.1em 0 .45em}} .dossier-cover p{{background:#1a1a1a;border-radius:2px;color:#f3ecd8;font-family:"Songti SC","Noto Serif SC",serif;font-size:.92rem;font-style:normal;line-height:1.75;margin:.2em 0 1.1em;padding:12px 16px}} .dossier-cover p::before{{color:#8a7e6a;content:"一句话结论";display:block;font:900 .55rem "Courier New",monospace;letter-spacing:.22em;margin-bottom:5px;text-transform:uppercase}} .dossier-meta{{display:none}} .dossier-actions{{display:flex;flex-wrap:wrap;gap:8px}} .dossier-actions a,.dossier-actions button{{background:#1a1a1a;border:1px solid #1a1a1a;border-radius:3px;color:#f3ecd8;font-size:.82rem;font-weight:800;padding:6px 14px;text-decoration:none}} .dossier-actions a.secondary,.dossier-actions button.secondary{{background:#fff;border-color:#1a1a1a;color:#1a1a1a}} .dossier-actions button{{line-height:1.9}}
.chapter-fold{{border-top:1px dotted var(--rule);margin:0;overflow:hidden}} .chapter-fold:last-child{{border-bottom:1px dotted var(--rule)}} .chapter-fold[open]{{margin:0 0 30px;overflow:visible}}
.chapter-summary{{cursor:pointer;list-style:none;display:flex;align-items:baseline;gap:.7em;flex-wrap:wrap;padding:17px 0;user-select:none}} .chapter-summary::-webkit-details-marker{{display:none}}
.chapter-summary .fold-label{{color:var(--accent);font-size:.82rem;font-weight:900;letter-spacing:.04em}} .chapter-summary .fold-label::before{{content:"▸ "}} .chapter-fold[open] .chapter-summary .fold-label::before{{content:"▾ "}}
.chapter-summary .fold-title{{color:var(--muted);font-size:.84rem}} .chapter-fold article{{padding:0 0 26px}} .chapter-fold[open] article{{padding:0 0 36px}}
article{{border-bottom:1px solid var(--rule);padding:0 0 50px;margin:0 0 50px}} article h1{{font-family:"Songti SC","Noto Serif SC",serif;font-size:2rem;line-height:1.35;margin-top:.2em}} h2{{font-family:"Songti SC","Noto Serif SC",serif;font-size:1.38rem;line-height:1.45;margin-top:2.2em}} h3{{font-size:1.08rem;margin-top:1.8em}} p{{margin:1em 0}} blockquote{{background:#e3dccd;border-left:4px solid var(--accent);margin:1.4em 0;padding:.7em 1.1em}} img{{display:block;height:auto;margin:2em auto;max-width:100%}} a{{color:#8f3d26}}
.focus-folders,.view-map{{border-top:1px dotted var(--rule);border-bottom:1px dotted var(--rule);margin:0 0 28px;padding:24px 0 30px}} .view-map{{border-top:0}} .view-map-kicker{{color:var(--accent);font:900 .7rem "Courier New",monospace;letter-spacing:.2em;margin-bottom:7px;text-transform:uppercase}} .section-head{{align-items:flex-start;display:flex;gap:12px;margin:0 0 16px}} .section-head>span{{background:#1a1a1a;border-radius:2px;color:#f3ecd8;display:inline-flex;flex:0 0 auto;font:900 10px "Courier New",monospace;height:24px;justify-content:center;letter-spacing:.04em;line-height:24px;width:28px}} .section-head h2,.view-map h2{{font-family:"Songti SC","Noto Serif SC",serif;font-size:1.9rem;line-height:1.35;margin:0 0 .2em}} .section-head p,.view-map p{{color:var(--muted);font-size:.9rem;line-height:1.7;margin:0;max-width:58ch}}
.folder-strip{{display:grid;gap:12px;grid-template-columns:repeat(3,minmax(0,1fr));margin:20px 0 22px}} .folder-card{{background:#f8f1df;border:1px solid #d7c9ad;border-radius:0 3px 3px 3px;box-shadow:0 4px 14px #00000014;color:#2b251f;min-height:104px;min-width:0;padding:20px 14px 14px;position:relative}} .folder-tab{{background:#b7552d;height:14px;left:-1px;position:absolute;top:-12px;width:42%;border-radius:3px 3px 0 0}} .folder-label{{color:#9c4927;font:900 .72rem "Courier New",monospace;letter-spacing:.08em;margin-bottom:8px}} .folder-card p{{display:-webkit-box;font-family:"Songti SC","Noto Serif SC",serif;font-size:.82rem;line-height:1.6;margin:0;color:inherit;overflow:hidden;-webkit-line-clamp:3;-webkit-box-orient:vertical}} .folder-1{{background:#24211d;border-color:#24211d;color:#f3ecd8}} .folder-1 .folder-label{{color:#f1c7ac}} .folder-1 .folder-tab{{background:#b7552d}} .folder-2 .folder-tab,.folder-3 .folder-tab{{background:#c9a06e}}
.clean-map{{margin:18px 0 0}} .clean-map-board{{background:#f4ecd9;border:1px solid #d9cebc;padding:18px}} .cork-board{{background:linear-gradient(135deg,#f4ecd9,#eee3cd);min-height:430px;overflow:hidden;position:relative}} .thread-line{{background:#b7552d;height:1px;left:14%;opacity:.24;position:absolute;top:50%;transform-origin:left center;width:72%;z-index:0}} .line-a{{transform:rotate(18deg)}} .line-b{{top:54%;transform:rotate(-17deg)}} .line-c{{left:28%;top:24%;transform:rotate(70deg);width:42%}} .clean-map-center{{background:#24211d;color:#fff;margin:0 auto 22px;max-width:300px;padding:18px 22px;position:relative;text-align:center;z-index:1}} .clean-map-center span{{color:#d8b48a;display:block;font-size:.72rem;font-weight:900;letter-spacing:.16em;margin-bottom:4px}} .clean-map-center strong{{font-size:1.08rem;line-height:1.35}}
.clean-map-grid{{display:grid;gap:14px;grid-template-columns:repeat(2,minmax(0,1fr));position:relative;z-index:1}} .cork-note{{background:#fffaf0;border:1px solid #d7c9ad;border-left:4px solid #b7552d;color:var(--ink);cursor:pointer;font:inherit;min-height:132px;min-width:0;overflow:hidden;padding:22px 14px 14px;position:relative;text-align:left;transform:rotate(-1.2deg);transition:transform .15s ease,box-shadow .15s ease;width:100%}} .cork-note:hover{{box-shadow:0 10px 24px #0002;transform:rotate(0deg) translateY(-2px)}} .cork-note.note-2{{background:#f6eedf;border-left-color:#c9a06e;transform:rotate(1.1deg)}} .cork-note.note-3{{background:#fbf3e2;border-left-color:#b7552d;transform:rotate(.7deg)}} .cork-note.note-4{{background:#f1e8d7;border-left-color:#9e7d55;transform:rotate(-.9deg)}} .pin{{background:#1b1815;border:1px solid #5a4635;border-radius:50%;height:9px;left:50%;position:absolute;top:7px;transform:translateX(-50%);width:9px}} .clean-map-index{{color:#b09f8d;font:900 .72rem "Courier New",monospace;letter-spacing:.08em}} .cork-note h3{{font-size:1rem;line-height:1.35;margin:.15em 0 .35em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}} .cork-note p{{color:#746d63;display:-webkit-box;font-size:.78rem;line-height:1.55;margin:0 0 .7em;overflow:hidden;-webkit-line-clamp:2;-webkit-box-orient:vertical}} .clean-map-source{{display:none}}
.quick-read,.matrix-section,.sources-section,.article-drawers{{border-top:1px dotted var(--rule);margin:0 0 28px;padding:24px 0 30px;position:relative;z-index:1}} .article-drawers h2{{font-family:"Songti SC","Noto Serif SC",serif;font-size:1.9rem;line-height:1.35;margin:.1em 0 .25em}} .article-drawers>p{{color:var(--muted);font-size:.92rem;margin:.4em 0 1.4em}} .quick-grid{{display:grid;gap:12px;grid-template-columns:repeat(2,minmax(0,1fr))}} .quick-card-lite{{background:#fffaf0;border:1px solid #d7c9ad;border-left:3px solid #b7552d;border-radius:3px;min-height:126px;padding:15px 16px}} .quick-number{{color:#a9957c;font:900 .68rem "Courier New",monospace;letter-spacing:.08em;margin-bottom:5px}} .quick-card-lite h3{{font-family:"Songti SC","Noto Serif SC",serif;font-size:1rem;line-height:1.45;margin:0 0 8px}} .quick-card-lite p{{color:#62594f;font-size:.82rem;line-height:1.65;margin:0}} .quick-card{{background:#fffef8;border:1px solid #d9cebc;border-radius:3px;min-width:0;padding:0}} .quick-card summary{{cursor:pointer;display:grid;gap:8px;grid-template-columns:34px 1fr;list-style:none;padding:14px 16px}} .quick-card summary::-webkit-details-marker{{display:none}} .quick-card summary span{{color:#b09f8d;font:900 .72rem "Courier New",monospace}} .quick-card summary strong{{font-family:"Songti SC","Noto Serif SC",serif;font-size:.96rem;line-height:1.45}} .quick-card p{{border-top:1px dotted var(--rule);color:#746d63;font-size:.8rem;line-height:1.7;margin:0;padding:12px 16px}} .quick-card a{{color:#a06a32;display:inline-block;font-size:.78rem;font-weight:900;margin:0 16px 14px;text-decoration:none}}
.matrix-stack{{display:grid;gap:10px}} .matrix-block,.source-card{{background:#fffaf0;border:1px solid #d7c9ad;border-left:3px solid #b7552d;border-radius:3px;padding:14px 16px}} .matrix-label{{color:#9c4927;font:900 .58rem "Courier New",monospace;letter-spacing:.18em;margin:0 0 5px;text-transform:uppercase}} .matrix-block h3,.source-card h3{{font-family:"Songti SC","Noto Serif SC",serif;font-size:1.02rem;line-height:1.45;margin:0 0 8px}} .matrix-block p,.source-card p,.matrix-block li,.source-card li{{color:#5f594f;font-size:.82rem;line-height:1.75;margin:.35em 0}} .matrix-block ul,.source-card ul,.matrix-block ol,.source-card ol{{margin:.35em 0;padding-left:1.2em}} .sources-stack{{display:block}} .source-card{{background:#fffaf0}} .source-card-wide{{max-width:none}}
.clean-map-source.source-audio b,.clean-map-source.source-intro b,.clean-map-source.source-web b,.clean-map-source.source-infer b{{background:#f3eee5;color:#756b5c}} .clean-map figcaption{{color:#948b80;font-size:.68rem;line-height:1.45;margin:10px 6px 0;text-align:center}}
table{{border-collapse:collapse;display:block;font-size:.86rem;line-height:1.65;margin:1.5em 0;overflow-x:auto;width:100%}} th,td{{border:1px solid var(--rule);padding:.78em .9em;text-align:left;vertical-align:top}} th{{background:#e3dccd;color:var(--ink);font-weight:900}} tr:nth-child(even) td{{background:#f7f1e6}}
.quote-action{{background:var(--accent);box-shadow:0 8px 24px #0003;display:none;position:fixed;z-index:20}} .quote-modal{{align-items:center;background:#181511cc;display:none;inset:0;justify-content:center;padding:20px;position:fixed;z-index:30}} .quote-dialog{{background:#eee8d9;max-height:94vh;max-width:620px;overflow:auto;padding:18px;width:100%}} .quote-dialog canvas{{background:#efe9dd;display:block;height:auto;width:100%}} .quote-tools{{display:flex;gap:10px;justify-content:flex-end;margin-top:14px}} .quote-tools .secondary{{background:#ded6c6;color:var(--ink)}} .quote-note{{color:var(--muted);font-size:.82rem;margin:0 0 12px}}
.source-legend{{display:none}} .source-badge{{display:none}} .relation-badge{{background:#efe6d3;border:1px solid #d7c9ad;border-radius:999px;color:#7a6046;display:inline-block;font-size:.66rem;font-weight:650;letter-spacing:.02em;line-height:1;margin:0 .22em;padding:.16em .42em;vertical-align:.08em;white-space:nowrap}} .relation-consensus,.relation-diff,.relation-conflict,.relation-partial,.relation-unique{{background:#efe6d3;color:#7a6046}}
@keyframes cursorBlink{{0%,45%{{opacity:1}}50%,95%{{opacity:0}}100%{{opacity:1}}}} @keyframes barPulse{{from{{height:5px;opacity:.45}}to{{height:28px;opacity:1}}}}
@media(max-width:900px){{.machine-cover{{display:block;padding:10px}} .machine-top{{padding:10px 22px}} .machine-leds{{display:none}} .machine-body{{padding:12px}} .machine-screen{{min-height:160px;padding:20px 18px}} .machine-controls{{grid-template-columns:1fr}} .layout{{display:block;padding:0 10px 72px}} aside{{display:none}} .search{{display:grid;grid-template-columns:1fr auto}} .web{{justify-self:start;width:auto}} nav{{display:none}} main{{padding:26px 20px 36px 58px}} .dossier-cover h1{{font-size:1.72rem}} .section-head h2,.view-map h2{{font-size:1.55rem}} article h1{{font-size:1.55rem}} .folder-strip,.clean-map-grid,.quick-grid,.sources-stack{{grid-template-columns:1fr}} .folder-strip{{gap:20px}} .clean-map-board{{padding:12px}} .cork-board{{min-height:auto}} .thread-line{{display:none}}}}
@media print{{body{{background:#fff}} .cover,aside{{display:none}} .layout{{display:block;padding:0}} main{{box-shadow:none;border:0}}}}
</style></head><body>
<header class="machine-cover" aria-label="播客研究所终端">
<section class="machine-frame">
<div class="machine-top">
<div class="machine-brand"><span class="machine-pi">PI</span><span>PODCAST INSTITUTE · 播客研究所</span></div>
<div class="machine-leds" aria-hidden="true"><span>PWR</span><span>SOURCE</span><span>ANALYZE</span><span>DOSSIER</span></div>
</div>
<div class="machine-body">
<div class="machine-screen">
<div class="machine-kicker">DOSSIER COMPLETE</div>
<div class="machine-title">档案装订完毕<span>▮</span></div>
<div class="machine-sub">DOSSIER READY · 可以开始探索</div>
<div class="machine-bars" aria-hidden="true">
<i style="height:16px"></i><i style="height:28px"></i><i style="height:10px"></i><i style="height:22px"></i><i style="height:34px"></i>
<i style="height:14px"></i><i style="height:26px"></i><i style="height:18px"></i><i style="height:31px"></i><i style="height:12px"></i>
<i style="height:24px"></i><i style="height:19px"></i><i style="height:30px"></i><i style="height:13px"></i><i style="height:27px"></i>
<i style="height:17px"></i><i style="height:21px"></i><i style="height:29px"></i><i style="height:11px"></i><i style="height:25px"></i>
</div>
<div class="machine-status">── ALL SYSTEMS OK ──</div>
</div>
<div class="machine-mode-label">MODE SELECT</div>
<div class="machine-controls">
<a href="http://127.0.0.1:8765/"><small>TRANSCRIPT</small>粘贴文稿</a>
<a href="http://127.0.0.1:8765/"><small>QUERY</small>输入问题</a>
<a href="http://127.0.0.1:8765/"><small>LINKS</small>粘贴链接</a>
</div>
</div>
</section>
</header>
<div class="layout"><aside><div class="search"><input id="query" type="search" placeholder="搜索书内内容">
<button id="searchButton">搜索</button></div><button class="web" id="webButton">联网检索选中内容</button>
<div class="source-legend"><strong>来源标注</strong><br>
<span class="source-badge source-audio">🎧 听稿</span>转写/文稿
<span class="source-badge source-intro">📝 简介</span>节目页
<span class="source-badge source-web">🌐 联网</span>公开资料
<span class="source-badge source-infer">🤔 推断</span>系统分析</div>
<nav><ol>{''.join(toc)}</ol>
<div class="toc-tools"><button id="sharePageSide" type="button">分享网页</button><a href="http://127.0.0.1:8765/">返回首页</a><a href="http://127.0.0.1:8765/library">网页书架</a></div></nav></aside><main><p class="empty" id="empty">没有找到相关章节。</p>
<section class="dossier-cover" aria-label="研究档案封面">
<div class="dossier-no">研究档案 · {datetime.now():%Y-%m-%d}</div>
<h1>{html.escape(title)}</h1>
<p>先看核心判断和观点地图，再按需要展开完整分析。</p>
<div class="dossier-meta"><span>{chapter_count:02d} sections</span><span>HTML dossier</span><span>可搜索 / 可分享</span></div>
<div class="dossier-actions"><a href="#focus-folders">看三个重点</a><a class="secondary" href="#viewpoint-map">看观点地图</a><a class="secondary" href="#article-drawers">完整分析</a><button class="secondary" id="sharePage" type="button">分享网页</button><a class="secondary" href="http://127.0.0.1:8765/">返回首页</a></div>
</section>
{viewpoint_map}
{quick_read_html}
{matrix_html}
{sources_html}
<section class="article-drawers" id="article-drawers" aria-label="完整分析">
<div class="section-head"><span>⑥</span><div><h2>完整分析</h2><p>正文默认收起。需要更多来源和论证时，再按章节展开。</p></div></div>
{''.join(sections)}</section></main></div>
<button class="quote-action" id="quoteAction">生成金句图</button>
<div class="quote-modal" id="quoteModal"><div class="quote-dialog">
<p class="quote-note">金句图会标注本书与章节来源，可直接下载或调用系统分享。</p>
<canvas id="quoteCanvas" width="1080" height="1440"></canvas>
<div class="quote-tools"><button class="secondary" id="quoteClose">关闭</button>
<button class="secondary" id="quoteDownload">下载 PNG</button><button id="quoteShare">分享</button></div>
</div></div>
<script>
const q=document.querySelector('#query'),articles=[...document.querySelectorAll('article')],empty=document.querySelector('#empty');
function search(){{const term=q.value.trim().toLowerCase();let shown=0;articles.forEach(a=>{{const hit=!term||a.innerText.toLowerCase().includes(term);
a.style.display=hit?'':'none';if(hit)shown++;
const fold=a.closest('details.chapter-fold');if(fold){{if(term&&hit)fold.open=true;else if(!term)fold.open=false;}}}});empty.style.display=shown?'none':'block';}}
document.querySelector('#searchButton').onclick=search;q.addEventListener('keydown',e=>{{if(e.key==='Enter')search();}});
document.querySelector('#webButton').onclick=()=>{{const term=window.getSelection().toString().trim()||q.value.trim();
if(!term){{q.focus();return;}}window.open('https://www.bing.com/search?q='+encodeURIComponent(term),'_blank','noopener');}};
async function shareCurrentPage(){{
  const payload={{title:'{html.escape(title)}',text:'播客研究所整理页',url:location.href}};
  if(navigator.share){{try{{await navigator.share(payload);return;}}catch(e){{}}}}
  try{{await navigator.clipboard.writeText(location.href);alert('链接已复制，可以粘贴分享。');}}
  catch(e){{prompt('复制这个链接分享：',location.href);}}
}}
document.querySelector('#sharePage')?.addEventListener('click',shareCurrentPage);
document.querySelector('#sharePageSide')?.addEventListener('click',shareCurrentPage);
const quoteAction=document.querySelector('#quoteAction'),quoteModal=document.querySelector('#quoteModal');
const quoteCanvas=document.querySelector('#quoteCanvas'),quoteCtx=quoteCanvas.getContext('2d');
let selectedQuote='',selectedChapter='';
function selectionInfo(){{
  const selection=window.getSelection();if(!selection||selection.isCollapsed)return null;
  const text=selection.toString().replace(/\\s+/g,' ').trim();if(!text)return null;
  const node=selection.anchorNode&&selection.anchorNode.nodeType===3?selection.anchorNode.parentElement:selection.anchorNode;
  const article=node&&node.closest?node.closest('article'):null;if(!article)return null;
  return {{text:text.slice(0,220),chapter:article.dataset.title||''}};
}}
function showQuoteAction(){{
  const info=selectionInfo();if(!info){{quoteAction.style.display='none';return;}}
  selectedQuote=info.text;selectedChapter=info.chapter;
  const range=window.getSelection().getRangeAt(0),rect=range.getBoundingClientRect();
  quoteAction.style.left=Math.max(12,Math.min(innerWidth-130,rect.left+rect.width/2-55))+'px';
  quoteAction.style.top=Math.max(12,rect.top-48)+'px';quoteAction.style.display='block';
}}
document.addEventListener('mouseup',()=>setTimeout(showQuoteAction,0));
document.addEventListener('touchend',()=>setTimeout(showQuoteAction,120));
document.addEventListener('mousedown',e=>{{if(e.target!==quoteAction)quoteAction.style.display='none';}});
function wrapText(ctx,text,maxWidth){{
  const lines=[];let line='';
  for(const char of text){{const test=line+char;if(ctx.measureText(test).width>maxWidth&&line){{lines.push(line);line=char;}}else line=test;}}
  if(line)lines.push(line);return lines;
}}
function drawQuoteCard(){{
  const c=quoteCanvas,x=120,maxWidth=840;c.width=1080;c.height=1440;
  quoteCtx.fillStyle='#efe9dd';quoteCtx.fillRect(0,0,c.width,c.height);
  quoteCtx.fillStyle='#b4492d';quoteCtx.fillRect(x,120,72,8);
  quoteCtx.font='500 30px -apple-system, BlinkMacSystemFont, \"PingFang SC\", sans-serif';
  quoteCtx.fillText('PODCAST NOTES',x,190);
  quoteCtx.fillStyle='#24211d';quoteCtx.font='bold 112px \"Songti SC\", \"Noto Serif CJK SC\", serif';
  quoteCtx.fillText('“',x-18,350);
  let size=selectedQuote.length>120?48:selectedQuote.length>70?56:64;
  quoteCtx.font='600 '+size+'px \"Songti SC\", \"Noto Serif CJK SC\", serif';
  const lines=wrapText(quoteCtx,selectedQuote,maxWidth),lineHeight=size*1.65;
  let y=390;for(const line of lines){{quoteCtx.fillText(line,x,y);y+=lineHeight;}}
  quoteCtx.fillStyle='#b4492d';quoteCtx.fillRect(x,Math.min(1130,y+50),120,5);
  quoteCtx.fillStyle='#514b43';quoteCtx.font='500 29px -apple-system, BlinkMacSystemFont, \"PingFang SC\", sans-serif';
  const sourceY=Math.min(1210,y+120);quoteCtx.fillText('《{html.escape(title)}》',x,sourceY);
  quoteCtx.fillStyle='#7b7369';quoteCtx.font='26px -apple-system, BlinkMacSystemFont, \"PingFang SC\", sans-serif';
  const chapterLines=wrapText(quoteCtx,selectedChapter,maxWidth);
  chapterLines.slice(0,2).forEach((line,i)=>quoteCtx.fillText(line,x,sourceY+48+i*40));
  quoteCtx.fillStyle='#24211d';quoteCtx.beginPath();quoteCtx.arc(900,1240,42,0,Math.PI*2);quoteCtx.fill();
  quoteCtx.fillStyle='#fff';quoteCtx.beginPath();quoteCtx.arc(887,1232,5,0,Math.PI*2);quoteCtx.fill();
  quoteCtx.beginPath();quoteCtx.arc(913,1232,5,0,Math.PI*2);quoteCtx.fill();
  quoteCtx.strokeStyle='#b4492d';quoteCtx.lineWidth=4;quoteCtx.beginPath();quoteCtx.moveTo(120,1330);
  quoteCtx.lineTo(960,1330);quoteCtx.stroke();
}}
quoteAction.onclick=()=>{{drawQuoteCard();quoteAction.style.display='none';quoteModal.style.display='flex';}};
document.querySelector('#quoteClose').onclick=()=>quoteModal.style.display='none';
quoteModal.onclick=e=>{{if(e.target===quoteModal)quoteModal.style.display='none';}};
function quoteBlob(){{return new Promise(resolve=>quoteCanvas.toBlob(resolve,'image/png'));}}
document.querySelector('#quoteDownload').onclick=async()=>{{const blob=await quoteBlob(),a=document.createElement('a');
a.href=URL.createObjectURL(blob);a.download='金句图-'+Date.now()+'.png';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}};
document.querySelector('#quoteShare').onclick=async()=>{{const blob=await quoteBlob(),file=new File([blob],'金句图.png',{{type:'image/png'}});
if(navigator.canShare&&navigator.canShare({{files:[file]}})){{await navigator.share({{files:[file],title:'{html.escape(title)}'}});}}
else{{const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='金句图.png';a.click();}}}};
</script></body></html>"""

    target = output or default_web_library(project) / f"{safe_filename(title)}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    write_library_index(target.parent)
    print(target)
    return target


def rewrite_and_collect_images(
    markdown_text: str, article_path: Path, book, used_names: set[str]
) -> str:
    from ebooklib import epub

    pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

    def replace(match: re.Match[str]) -> str:
        alt, raw_path = match.group(1), match.group(2).strip().strip("<>")
        if raw_path.startswith(("http://", "https://", "data:")):
            return match.group(0)
        source = (article_path.parent / raw_path).resolve()
        if not source.exists():
            raise FileNotFoundError(f"插图不存在：{source}")
        digest = hashlib.sha1(str(source).encode()).hexdigest()[:10]
        filename = f"images/{digest}-{source.name}"
        if filename not in used_names:
            book.add_item(
                epub.EpubItem(
                    uid=f"img-{digest}",
                    file_name=filename,
                    media_type=media_type(source),
                    content=source.read_bytes(),
                )
            )
            used_names.add(filename)
        return f"![{alt}]({filename})"

    return pattern.sub(replace, markdown_text)


def build(project: Path, title: str, output: Path) -> None:
    from ebooklib import epub

    ordered = ordered_markdown_files(project)

    book = epub.EpubBook()
    book.set_identifier(f"xyz-{datetime.now():%Y%m%d%H%M%S}")
    book.set_title(title)
    book.set_language("zh-CN")
    book.add_author("个人播客收藏")
    style = epub.EpubItem(
        uid="style",
        file_name="style/book.css",
        media_type="text/css",
        content="""
@page { margin: 7%; }
body { color:#202020; font-family:"Songti SC","Noto Serif CJK SC",serif;
font-size:1em; line-height:1.85; text-align:justify; }
h1,h2,h3 { color:#171717; font-family:"PingFang SC","Noto Sans CJK SC",sans-serif;
line-height:1.35; text-align:left; }
h1 { font-size:1.8em; margin:12% 0 2em; page-break-before:always; }
h2 { border-bottom:1px solid #d7d0c5; font-size:1.25em; margin:2.4em 0 1em;
padding-bottom:.35em; }
p { margin:.8em 0; orphans:2; widows:2; }
blockquote { background:#f5f2ec; border-left:3px solid #a88b5d; color:#4b453d;
margin:1.5em 0; padding:.7em 1em; }
img { display:block; height:auto; margin:2em auto; max-width:100%; page-break-inside:avoid; }
li { margin:.45em 0; }
a { color:#6e5634; text-decoration:none; }
.title-page { page-break-after:always; text-align:center; padding-top:28%; }
.title-page h1 { font-size:2.2em; letter-spacing:.08em; margin:0 0 1em;
page-break-before:auto; text-align:center; }
.rule { background:#a88b5d; height:2px; margin:2.2em auto; width:28%; }
.meta { color:#8a8a8a; font-size:.8em; }
""",
    )
    book.add_item(style)
    chapters, used_images = [], set()

    cover = epub.EpubHtml(title="封面", file_name="title.xhtml", lang="zh-CN")
    cover.content = (
        f'<html><body><section class="title-page"><h1>{html.escape(title)}</h1>'
        '<div class="rule"></div><p>小宇宙播客整理 · 图文阅读版</p>'
        f'<p class="meta">{datetime.now():%Y.%m}</p></section></body></html>'
    )
    cover.add_item(style)
    book.add_item(cover)
    chapters.append(cover)

    for index, path in enumerate(ordered, 1):
        raw = path.read_text(encoding="utf-8")
        raw = rewrite_and_collect_images(raw, path, book, used_images)
        chapter = epub.EpubHtml(
            title=chapter_title(raw, path.stem),
            file_name=f"chapter_{index}.xhtml",
            lang="zh-CN",
        )
        chapter.content = f"<html><body>{markdown_html(raw)}</body></html>"
        chapter.add_item(style)
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]
    output.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(output), book)
    with zipfile.ZipFile(output) as archive:
        error = archive.testzip()
        if error:
            raise RuntimeError(f"EPUB 损坏：{error}")
    print(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_parser = sub.add_parser("fetch")
    fetch_parser.add_argument("--episodes", type=Path, required=True)
    fetch_parser.add_argument("--cache", type=Path, required=True)
    transcribe_parser = sub.add_parser("transcribe")
    transcribe_parser.add_argument("--cache", type=Path, required=True)
    transcribe_parser.add_argument("--model", default=DEFAULT_MODEL)
    cloud_parser = sub.add_parser("transcribe-cloud")
    cloud_parser.add_argument("--episodes", type=Path, required=True)
    cloud_parser.add_argument("--cache", type=Path, required=True)
    groq_parser = sub.add_parser("transcribe-groq")
    groq_parser.add_argument("--episodes", type=Path, required=True)
    groq_parser.add_argument("--cache", type=Path, required=True)
    groq_parser.add_argument("--model", default="whisper-large-v3-turbo")
    auto_parser = sub.add_parser("transcribe-auto")
    auto_parser.add_argument("--episodes", type=Path, required=True)
    auto_parser.add_argument("--cache", type=Path, required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--project", type=Path, required=True)
    build_parser.add_argument("--title", required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    html_parser = sub.add_parser("build-html")
    html_parser.add_argument("--project", type=Path, required=True)
    html_parser.add_argument("--title", required=True)
    html_parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "fetch":
        fetch(args.episodes, args.cache)
    elif args.command == "transcribe":
        transcribe(args.cache, args.model)
    elif args.command == "transcribe-cloud":
        transcribe_cloud(args.episodes, args.cache)
    elif args.command == "transcribe-groq":
        transcribe_groq(args.episodes, args.cache, args.model)
    elif args.command == "transcribe-auto":
        transcribe_auto(args.episodes, args.cache)
    elif args.command == "build":
        build(args.project, args.title, args.output)
    else:
        build_html(args.project, args.title, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
