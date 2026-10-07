#!/usr/bin/env python3
"""Magic-mode configuration and durable background image generation."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request


DEFAULT_MAGIC_MODES = [
    {
        "id": "cheese",
        "title": "Cheese",
        "prompt": "Change nothing about this photo except turn everything into cheese.",
    }
]
TITLE_MAX = 18
PROMPT_MAX = 2000
SOURCE_IMAGE_SIZE = 512
SOURCE_JPEG_QUALITY = 95
MAX_AUTO_ATTEMPTS = 3


def read_env_file(path: str | Path) -> dict[str, str]:
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value.strip().strip('"').strip("'")
    return values


def load_env_file(path: str | Path, *, override: bool = False) -> None:
    """Load simple KEY=VALUE settings, optionally refreshing existing values."""
    for key, value in read_env_file(path).items():
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def mask_api_key(value: str) -> str:
    value = str(value or "").strip()
    return "********" if value else ""


def read_magic_connection_settings(path: str | Path) -> dict[str, object]:
    values = read_env_file(path)
    api_key = values.get("OPENAI_API_KEY", "").strip()
    return {
        "api_key_configured": bool(api_key),
        "api_key_masked": mask_api_key(api_key),
        "proxy": values.get("OPTOCAM_MAGIC_PROXY", "").strip(),
    }


def save_magic_connection_settings(
    path: str | Path,
    *,
    api_key: str | None = None,
    clear_api_key: bool = False,
    proxy: str | None = None,
) -> dict[str, object]:
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    updates = {}
    if clear_api_key:
        updates["OPENAI_API_KEY"] = ""
    elif api_key is not None and api_key.strip():
        updates["OPENAI_API_KEY"] = api_key.strip().replace("\r", "").replace("\n", "")
    if proxy is not None:
        cleaned_proxy = proxy.strip().replace("\r", "").replace("\n", "")
        if cleaned_proxy and not re.match(r"^https?://", cleaned_proxy, re.I):
            raise ValueError("Proxy must start with http:// or https://")
        updates["OPTOCAM_MAGIC_PROXY"] = cleaned_proxy

    seen = set()
    output = []
    for line in lines:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match.group(1) if match else None
        if key in updates:
            if key not in seen:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    return read_magic_connection_settings(path)


def _atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def normalize_magic_modes(value) -> list[dict[str, str]]:
    if not isinstance(value, list):
        value = []
    result = []
    used = set()
    for index, entry in enumerate(value[:24], start=1):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()[:TITLE_MAX]
        prompt = str(entry.get("prompt") or "").strip()[:PROMPT_MAX]
        if not title or not prompt:
            continue
        raw_id = re.sub(r"[^a-z0-9]+", "-", str(entry.get("id") or title).lower()).strip("-")
        raw_id = raw_id or f"magic-{index}"
        mode_id = raw_id
        suffix = 2
        while mode_id in used:
            mode_id = f"{raw_id}-{suffix}"
            suffix += 1
        used.add(mode_id)
        result.append({"id": mode_id, "title": title, "prompt": prompt})
    return result


class MagicModeStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        if not self.path.exists():
            self.save(DEFAULT_MAGIC_MODES)

    def load(self) -> list[dict[str, str]]:
        with self._lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = DEFAULT_MAGIC_MODES
        return normalize_magic_modes(value)

    def save(self, value) -> list[dict[str, str]]:
        modes = normalize_magic_modes(value)
        with self._lock:
            _atomic_json(self.path, modes)
        return modes

    def get(self, mode_id: str) -> dict[str, str] | None:
        return next((mode for mode in self.load() if mode["id"] == mode_id), None)


def is_ai_generated(path: str | Path) -> bool:
    return Path(str(path) + ".ai.json").is_file()


def read_magic_source_metadata(path: str | Path) -> dict[str, object] | None:
    """Read persistent state for an original photo captured in Magic mode."""
    try:
        value = json.loads(Path(str(path) + ".magic.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("magic_source") is True else None


class OpenAIImageEditor:
    """Small dependency-free client for the OpenAI Images edit endpoint."""

    def __init__(self, config: dict[str, str] | None = None):
        config = config or {}
        self.api_key = str(config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY", "")).strip()
        self.model = str(
            config.get("OPTOCAM_MAGIC_MODEL")
            or os.getenv("OPTOCAM_MAGIC_MODEL", "chatgpt-image-latest")
        ).strip()
        self.quality = str(
            config.get("OPTOCAM_MAGIC_QUALITY")
            or os.getenv("OPTOCAM_MAGIC_QUALITY")
            or "high"
        ).strip()
        self.input_fidelity = str(
            config.get("OPTOCAM_MAGIC_INPUT_FIDELITY")
            or os.getenv("OPTOCAM_MAGIC_INPUT_FIDELITY")
            or "high"
        ).strip()
        self.output_compression = str(
            config.get("OPTOCAM_MAGIC_OUTPUT_COMPRESSION")
            or os.getenv("OPTOCAM_MAGIC_OUTPUT_COMPRESSION")
            or "95"
        ).strip()
        self.size = str(
            config.get("OPTOCAM_MAGIC_SIZE") or os.getenv("OPTOCAM_MAGIC_SIZE") or "1024x1024"
        ).strip()
        self.timeout = float(
            config.get("OPTOCAM_MAGIC_TIMEOUT") or os.getenv("OPTOCAM_MAGIC_TIMEOUT", "120")
        )
        self.proxy = str(
            config.get("OPTOCAM_MAGIC_PROXY") or os.getenv("OPTOCAM_MAGIC_PROXY", "")
        ).strip()

    @staticmethod
    def _prepare_source_image(image_path: Path) -> bytes:
        """Return a 512-square JPEG for upload without modifying the original."""
        from PIL import Image, ImageOps

        with Image.open(image_path) as image:
            # JPEG draft decoding avoids expanding the 2592-square camera file
            # at full resolution just to reduce it for the API request.
            image.draft("RGB", (SOURCE_IMAGE_SIZE, SOURCE_IMAGE_SIZE))
            image = ImageOps.exif_transpose(image).convert("RGB")
            image = ImageOps.fit(
                image,
                (SOURCE_IMAGE_SIZE, SOURCE_IMAGE_SIZE),
                method=Image.LANCZOS,
                centering=(0.5, 0.5),
            )
            output = io.BytesIO()
            image.save(output, "JPEG", quality=SOURCE_JPEG_QUALITY, optimize=True)
            return output.getvalue()

    @staticmethod
    def _multipart(fields: dict[str, str], image_path: Path, boundary: str) -> bytes:
        chunks = []
        marker = boundary.encode("ascii")
        for name, value in fields.items():
            chunks.extend([
                b"--" + marker + b"\r\n",
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"), b"\r\n",
            ])
        chunks.extend([
            b"--" + marker + b"\r\n",
            b'Content-Disposition: form-data; name="image"; filename="source.jpg"\r\n',
            b"Content-Type: image/jpeg\r\n\r\n",
            OpenAIImageEditor._prepare_source_image(image_path), b"\r\n",
            b"--" + marker + b"--\r\n",
        ])
        return b"".join(chunks)

    def _request_fields(self, prompt: str) -> dict[str, str]:
        fields = {
            "model": self.model,
            "prompt": (
                "Use the attached camera photo as the source. Keep the scene recognizable and coherent. "
                f"Apply this transformation: {prompt}"
            ),
            "quality": self.quality,
            "size": self.size,
            "output_format": "jpeg",
            "output_compression": self.output_compression,
        }
        if self.model not in {"gpt-image-2", "gpt-image-2-2026-04-21"}:
            fields["input_fidelity"] = self.input_fidelity
        return fields

    def edit(self, source_path: Path, prompt: str) -> bytes:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        boundary = "----optocam-" + uuid.uuid4().hex
        fields = self._request_fields(prompt)
        body = self._multipart(fields, source_path, boundary)
        api_request = request.Request(
            "https://api.openai.com/v1/images/edits",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            opener = (
                request.build_opener(
                    request.ProxyHandler({"http": self.proxy, "https": self.proxy})
                )
                if self.proxy else request.build_opener()
            )
            with opener.open(api_request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"OpenAI image edit failed ({exc.code}): {detail}") from exc
        data = payload.get("data") or []
        encoded = None
        if data and isinstance(data[0], dict):
            encoded = data[0].get("b64_json") or data[0].get("image_base64")
        if not encoded:
            raise RuntimeError("OpenAI returned no image data")
        return base64.b64decode(encoded)


class MagicGenerationWorker:
    """Disk-backed, single-worker generation queue that survives restarts."""

    def __init__(self, home: str | Path, output_name_factory, on_ready=None, on_error=None):
        self.home = Path(home)
        self.photos_dir = self.home / "photos"
        self.queue_dir = self.home / "magic_queue"
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.store = MagicModeStore(self.home / "magic_modes.json")
        self.output_name_factory = output_name_factory
        self.on_ready = on_ready
        self.on_error = on_error
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="magic-generation", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def enqueue(self, source_path: str | Path, mode_id: str) -> bool:
        mode = self.store.get(mode_id)
        if mode is None:
            return False
        source_path = Path(source_path).resolve()
        job_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        created_at = datetime.now(timezone.utc).isoformat()
        job = {
            "id": job_id,
            "source_path": str(source_path),
            "mode_id": mode["id"],
            "title": mode["title"],
            "prompt": mode["prompt"],
            "created_at": created_at,
            "attempts": 0,
            "next_attempt_at": 0,
        }
        _atomic_json(self.queue_dir / f"{job_id}.json", job)
        self._write_source_state(source_path, job, "queued")
        self._wake.set()
        return True

    @staticmethod
    def _write_source_state(source: Path, job: dict, status: str, **extra) -> None:
        # A photo deleted while an API request is in flight must stay deleted;
        # do not recreate an orphan sidecar when that request finishes.
        if not source.is_file():
            return
        payload = {
            "magic_source": True,
            "status": status,
            "job_id": job.get("id"),
            "magic_mode_id": job.get("mode_id"),
            "magic_mode_title": job.get("title"),
            "created_at": job.get("created_at"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        payload.update(extra)
        _atomic_json(Path(str(source) + ".magic.json"), payload)

    def _next_job(self):
        now = time.time()
        for path in sorted(self.queue_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("status") == "failed":
                    continue
                if float(payload.get("next_attempt_at") or 0) <= now:
                    return path, payload
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return None

    def _process(self, path: Path, job: dict) -> None:
        source = Path(str(job.get("source_path") or ""))
        if not source.is_file():
            raise RuntimeError("source photo is missing")
        self._write_source_state(source, job, "processing")
        editor = OpenAIImageEditor(read_env_file(self.home / ".env"))
        image_bytes = editor.edit(source, str(job["prompt"]))
        if len(image_bytes) < 1000 or not image_bytes.startswith(b"\xff\xd8"):
            raise RuntimeError("OpenAI returned an invalid JPEG")
        output_name = self.output_name_factory()
        output = self.photos_dir / output_name
        temporary = output.with_suffix(".jpg.tmp")
        temporary.write_bytes(image_bytes)
        temporary.replace(output)
        _atomic_json(Path(str(output) + ".ai.json"), {
            "ai_generated": True,
            "source": source.name,
            "magic_mode_id": job["mode_id"],
            "magic_mode_title": job["title"],
            "prompt": job["prompt"],
            "model": editor.model,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        self._write_source_state(source, job, "complete", output=output.name)
        path.unlink(missing_ok=True)
        if self.on_ready:
            self.on_ready(str(output), dict(job))

    def _record_failure(self, path: Path, job: dict, exc: Exception) -> bool:
        attempts = int(job.get("attempts") or 0) + 1
        job["attempts"] = attempts
        job["last_error"] = str(exc)[:500]
        failed = attempts >= MAX_AUTO_ATTEMPTS
        job["status"] = "failed" if failed else "retrying"
        job["next_attempt_at"] = (
            0 if failed
            else time.time() + min(300, 15 * (2 ** min(attempts - 1, 4)))
        )
        _atomic_json(path, job)
        source = Path(str(job.get("source_path") or ""))
        self._write_source_state(
            source, job, job["status"], attempts=attempts,
            last_error=job["last_error"],
        )
        if self.on_error:
            self.on_error(exc, dict(job))
        return failed

    def _run(self) -> None:
        while not self._stop.is_set():
            next_job = self._next_job()
            if next_job is None:
                self._wake.clear()
                self._wake.wait(5)
                continue
            path, job = next_job
            try:
                self._process(path, job)
            except Exception as exc:
                failed = self._record_failure(path, job, exc)
                if not failed:
                    self._wake.wait(min(5, max(1, job["next_attempt_at"] - time.time())))


def retry_magic_generation(home: str | Path, source_path: str | Path) -> dict:
    """Reset a failed/pending retry job so the camera worker runs it now."""
    home = Path(home).resolve()
    source = Path(source_path).resolve()
    photos_dir = (home / "photos").resolve()
    if source.parent != photos_dir or not source.is_file():
        raise FileNotFoundError("Magic source photo was not found")

    metadata = read_magic_source_metadata(source)
    if not metadata or metadata.get("status") not in {"failed", "retrying"}:
        raise ValueError("Magic photo is not waiting for a retry")
    job_id = str(metadata.get("job_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9-]+", job_id):
        raise FileNotFoundError("Magic retry job was not found")

    job_path = home / "magic_queue" / f"{job_id}.json"
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise FileNotFoundError("Magic retry job was not found") from exc
    if Path(str(job.get("source_path") or "")).resolve() != source:
        raise ValueError("Magic retry job does not match this photo")

    job["attempts"] = 0
    job["next_attempt_at"] = 0
    job["status"] = "queued"
    job.pop("last_error", None)
    _atomic_json(job_path, job)
    MagicGenerationWorker._write_source_state(source, job, "queued", attempts=0)
    return dict(job)
