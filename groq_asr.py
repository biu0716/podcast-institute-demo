#!/usr/bin/env python3
"""Groq ASR client with optional API key stored in macOS Keychain."""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


KEYCHAIN_SERVICE = "com.biu.podcast-research.groq-asr"
KEYCHAIN_USER = "podcast-research-assistant"
ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
MODELS_ENDPOINT = "https://api.groq.com/openai/v1/models"
DEFAULT_MODEL = "whisper-large-v3-turbo"
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
CHUNK_SECONDS = 900


class GroqAsrError(RuntimeError):
    pass


def _keychain_account(field: str) -> str:
    return f"{KEYCHAIN_USER}.{field}"


def keychain_set(field: str, value: str) -> None:
    if not Path("/usr/bin/security").exists():
        raise GroqAsrError("当前环境不支持 macOS Keychain。线上部署请使用 GROQ_API_KEY 环境变量。")
    subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            _keychain_account(field),
            "-w",
            value,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def keychain_get(field: str) -> str | None:
    if not Path("/usr/bin/security").exists():
        return None
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            _keychain_account(field),
            "-w",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def save_api_key(api_key: str) -> None:
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("Groq API Key 不能为空")
    keychain_set("api-key", api_key)


def load_api_key() -> str:
    api_key = os.environ.get("GROQ_API_KEY") or keychain_get("api-key")
    if not api_key:
        raise GroqAsrError("Groq API Key 尚未配置")
    return api_key


def credentials_status() -> dict:
    env_key = os.environ.get("GROQ_API_KEY")
    keychain_key = keychain_get("api-key")
    api_key = env_key or keychain_key
    return {
        "configured": bool(api_key),
        "source": "env" if env_key else ("keychain" if keychain_key else ""),
        "api_key_hint": f"{api_key[:7]}…{api_key[-4:]}" if api_key else "",
    }


def test_credentials() -> dict:
    try:
        import httpx

        api_key = load_api_key()
        response = httpx.get(
            MODELS_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
        if response.status_code >= 400:
            return {"ok": False, "message": response.text[:500]}
        return {"ok": True, "message": "Groq API Key 可用"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}


def transcribe_audio_url(
    audio_url: str,
    *,
    model: str = DEFAULT_MODEL,
    language: str = "zh",
) -> dict:
    import httpx

    api_key = load_api_key()
    multipart_fields = {
        "model": (None, model),
        "url": (None, audio_url),
        "language": (None, language),
        "response_format": (None, "verbose_json"),
        "temperature": (None, "0"),
        "timestamp_granularities[]": (None, "segment"),
    }
    response = httpx.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}"},
        files=multipart_fields,
        timeout=900,
    )
    if response.status_code >= 400:
        raise GroqAsrError(f"Groq ASR 返回 HTTP {response.status_code}：{response.text[:800]}")
    return response.json()


def transcribe_audio_file(
    audio_path: Path,
    *,
    model: str = DEFAULT_MODEL,
    language: str = "zh",
) -> dict:
    import httpx

    api_key = load_api_key()
    mime_type = mimetypes.guess_type(audio_path.name)[0] or "audio/mpeg"
    with audio_path.open("rb") as file:
        multipart_fields = [
            ("file", (audio_path.name, file, mime_type)),
            ("model", (None, model)),
            ("language", (None, language)),
            ("response_format", (None, "verbose_json")),
            ("temperature", (None, "0")),
            ("timestamp_granularities[]", (None, "segment")),
        ]
        response = httpx.post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}"},
            files=multipart_fields,
            timeout=900,
        )
    if response.status_code >= 400:
        raise GroqAsrError(f"Groq ASR 返回 HTTP {response.status_code}：{response.text[:800]}")
    return response.json()


def is_file_too_large_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "file too large" in message or "size limit" in message or "maximum" in message


def audio_suffix_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".mp3", ".m4a", ".mp4", ".mpeg", ".mpga", ".wav", ".webm", ".ogg", ".flac"}:
        return suffix
    return ".mp3"


def download_audio(audio_url: str, target: Path) -> Path:
    import httpx

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    with httpx.stream("GET", audio_url, follow_redirects=True, timeout=900) as response:
        response.raise_for_status()
        with temporary.open("wb") as output:
            for chunk in response.iter_bytes(1024 * 1024):
                if chunk:
                    output.write(chunk)
    temporary.replace(target)
    return target


def ensure_local_audio(item: dict, folder: Path) -> Path:
    audio_path = item.get("audio_path")
    if audio_path:
        path = Path(audio_path)
        if path.exists():
            return path
    for candidate in sorted(folder.glob("audio.*")):
        if candidate.is_file() and not candidate.name.endswith(".part"):
            return candidate
    suffix = audio_suffix_from_url(item["audio_url"])
    return download_audio(item["audio_url"], folder / f"audio{suffix}")


def chunk_audio(audio_path: Path, chunks_dir: Path) -> list[tuple[Path, float]]:
    if not shutil.which("ffmpeg"):
        raise GroqAsrError("需要安装 ffmpeg 才能把大音频切片后提交 Groq。")
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunks_dir / "chunk_%04d.mp3"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "32k",
        "-f",
        "segment",
        "-segment_time",
        str(CHUNK_SECONDS),
        "-reset_timestamps",
        "1",
        str(pattern),
    ]
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    chunks = sorted(chunks_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise GroqAsrError("ffmpeg 没有生成可转写的音频切片。")
    oversized = [path for path in chunks if path.stat().st_size > MAX_UPLOAD_BYTES]
    if oversized:
        names = ", ".join(path.name for path in oversized[:3])
        raise GroqAsrError(f"音频切片后仍超过 Groq 大小限制：{names}")
    return [(path, index * CHUNK_SECONDS) for index, path in enumerate(chunks)]


def merge_chunk_payloads(payloads: list[tuple[dict, float, Path]]) -> dict:
    merged_text: list[str] = []
    merged_segments: list[dict] = []
    chunks: list[dict] = []
    for payload, offset, path in payloads:
        text = (payload.get("text") or "").strip()
        if text:
            merged_text.append(text)
        for segment in payload.get("segments") or []:
            item = dict(segment)
            if isinstance(item.get("start"), (int, float)):
                item["start"] = round(float(item["start"]) + offset, 3)
            if isinstance(item.get("end"), (int, float)):
                item["end"] = round(float(item["end"]) + offset, 3)
            item["chunk"] = path.name
            merged_segments.append(item)
        chunks.append(
            {
                "file": str(path),
                "start": offset,
                "bytes": path.stat().st_size,
                "text_length": len(text),
            }
        )
    return {
        "text": "\n".join(merged_text),
        "segments": merged_segments,
        "_chunked": True,
        "_chunks": chunks,
    }


def transcribe_with_local_fallback(item: dict, folder: Path, *, model: str) -> dict:
    audio_path = ensure_local_audio(item, folder)
    if audio_path.stat().st_size <= MAX_UPLOAD_BYTES:
        return transcribe_audio_file(audio_path, model=model)

    print(
        f"音频超过 Groq 单文件限制，自动切片：{item.get('title') or item['eid']} "
        f"({audio_path.stat().st_size / 1024 / 1024:.1f} MB)"
    )
    chunks = chunk_audio(audio_path, folder / "groq-chunks")
    payloads: list[tuple[dict, float, Path]] = []
    for index, (chunk, offset) in enumerate(chunks, 1):
        print(f"  Groq 分片转写 {index}/{len(chunks)}：{chunk.name}")
        payloads.append((transcribe_audio_file(chunk, model=model), offset, chunk))
    return merge_chunk_payloads(payloads)


def transcribe_many(
    items: list[dict],
    output_root: Path,
    *,
    workers: int = 3,
    model: str = DEFAULT_MODEL,
) -> list[dict]:
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    def run(item: dict) -> dict:
        folder = output_root / item["eid"]
        try:
            payload = transcribe_audio_url(item["audio_url"], model=model)
        except GroqAsrError as exc:
            if not is_file_too_large_error(exc):
                raise
            payload = transcribe_with_local_fallback(item, folder, model=model)
        target = output_root / item["eid"] / "transcript.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        text = payload.get("text") or ""
        segments = payload.get("segments") or []
        transcript = {
            "provider": "groq-asr",
            "model": model,
            "text": text,
            "segments": segments,
            "chunked": bool(payload.get("_chunked")),
            "chunks": payload.get("_chunks", []),
        }
        if not payload.get("_chunked"):
            transcript["raw"] = payload
        target.write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {**item, "transcript": str(target)}

    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as executor:
        futures = {executor.submit(run, item): item for item in items}
        for future in as_completed(futures):
            results.append(future.result())
    return results
