#!/usr/bin/env python3
"""Tencent Cloud ASR client with credentials stored in macOS Keychain."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


KEYCHAIN_SERVICE = "com.biu.podcast-research.tencent-asr"
KEYCHAIN_USER = "podcast-research-assistant"
HOST = "asr.tencentcloudapi.com"
ENDPOINT = f"https://{HOST}"
SERVICE = "asr"
VERSION = "2019-06-14"
REGION = "ap-shanghai"


class TencentAsrError(RuntimeError):
    pass


def _keychain_account(field: str) -> str:
    return f"{KEYCHAIN_USER}.{field}"


def keychain_set(field: str, value: str) -> None:
    if not Path("/usr/bin/security").exists():
        raise TencentAsrError("当前环境不支持 macOS Keychain。线上部署请使用 TENCENT_SECRET_ID / TENCENT_SECRET_KEY 环境变量。")
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


def save_credentials(secret_id: str, secret_key: str) -> None:
    secret_id, secret_key = secret_id.strip(), secret_key.strip()
    if not secret_id or not secret_key:
        raise ValueError("SecretId 和 SecretKey 都不能为空")
    keychain_set("secret-id", secret_id)
    keychain_set("secret-key", secret_key)


def load_credentials() -> tuple[str, str]:
    secret_id = os.environ.get("TENCENT_SECRET_ID") or keychain_get("secret-id")
    secret_key = os.environ.get("TENCENT_SECRET_KEY") or keychain_get("secret-key")
    if not secret_id or not secret_key:
        raise TencentAsrError("腾讯云 ASR 密钥尚未配置")
    return secret_id, secret_key


def credentials_status() -> dict:
    env_secret_id = os.environ.get("TENCENT_SECRET_ID")
    env_secret_key = os.environ.get("TENCENT_SECRET_KEY")
    secret_id = env_secret_id or keychain_get("secret-id")
    secret_key = env_secret_key or keychain_get("secret-key")
    return {
        "configured": bool(secret_id and secret_key),
        "source": "env" if env_secret_id and env_secret_key else ("keychain" if secret_id and secret_key else ""),
        "secret_id_hint": f"{secret_id[:6]}…{secret_id[-4:]}" if secret_id else "",
    }


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def api_request(action: str, payload: dict) -> dict:
    secret_id, secret_key = load_credentials()
    timestamp = int(time.time())
    date = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime("%Y-%m-%d")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    content_type = "application/json; charset=utf-8"

    canonical_headers = f"content-type:{content_type}\nhost:{HOST}\n"
    signed_headers = "content-type;host"
    canonical_request = "\n".join(
        [
            "POST",
            "/",
            "",
            canonical_headers,
            signed_headers,
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
        ]
    )
    credential_scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = "\n".join(
        [
            "TC3-HMAC-SHA256",
            str(timestamp),
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _sign(secret_date, SERVICE)
    secret_signing = _sign(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    authorization = (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": HOST,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": VERSION,
        "X-TC-Region": REGION,
    }
    request = urllib.request.Request(
        ENDPOINT, data=body.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise TencentAsrError(f"腾讯云接口返回 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise TencentAsrError(f"无法连接腾讯云 ASR：{exc.reason}") from exc

    response = result.get("Response", {})
    error = response.get("Error")
    if error:
        raise TencentAsrError(f'{error.get("Code", "Unknown")}：{error.get("Message", "")}')
    return response


def test_credentials() -> dict:
    try:
        response = api_request("DescribeTaskStatus", {"TaskId": 1})
        return {"ok": True, "message": "密钥和语音识别服务可用", "request_id": response.get("RequestId")}
    except TencentAsrError as exc:
        message = str(exc)
        if "NoSuchTask" in message:
            return {"ok": True, "message": "密钥和语音识别服务可用"}
        return {"ok": False, "message": message}


def submit_audio(audio_url: str) -> int:
    response = api_request(
        "CreateRecTask",
        {
            "EngineModelType": "16k_zh",
            "ChannelNum": 1,
            "ResTextFormat": 3,
            "SourceType": 0,
            "Url": audio_url,
        },
    )
    return int(response["Data"]["TaskId"])


def poll_task(task_id: int, timeout: int = 600, interval: int = 5) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = api_request("DescribeTaskStatus", {"TaskId": task_id})["Data"]
        status = int(data.get("Status", -1))
        if status == 2:
            return data
        if status == 3:
            raise TencentAsrError(data.get("ErrorMsg") or f"任务 {task_id} 识别失败")
        time.sleep(interval)
    raise TencentAsrError(f"任务 {task_id} 等待超过 {timeout} 秒")


def transcribe_many(items: list[dict], output_root: Path, workers: int = 5) -> list[dict]:
    output_root.mkdir(parents=True, exist_ok=True)
    submitted = []
    for item in items:
        task_id = submit_audio(item["audio_url"])
        submitted.append({**item, "task_id": task_id})

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(submitted))) as executor:
        futures = {
            executor.submit(poll_task, item["task_id"]): item for item in submitted
        }
        for future in as_completed(futures):
            item = futures[future]
            data = future.result()
            target = output_root / item["eid"] / "transcript.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "provider": "tencent-cloud-asr",
                "task_id": item["task_id"],
                "text": data.get("Result", ""),
                "segments": data.get("ResultDetail", []),
                "audio_duration": data.get("AudioDuration", 0),
            }
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            results.append({**item, "transcript": str(target)})
    return results
