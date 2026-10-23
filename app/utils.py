import os
import re
import uuid
import asyncio
from urllib.parse import quote, urlparse
from fastapi import Request, HTTPException
from starlette.responses import Response, FileResponse

from app.config import QUOTA_BYTES, MAX_UPLOAD_BYTES

UPLOAD_DIR = "uploads"
PDF_DIR = os.path.join(UPLOAD_DIR, "pdf")
AUDIO_DIR = os.path.join(UPLOAD_DIR, "audio")

os.makedirs(PDF_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".wma", ".aiff", ".opus"}
_CHUNK = 1024 * 1024


class UploadError(Exception):
    """Raised with a user-facing (Danish) message when an upload is rejected."""


def get_storage_usage() -> dict:
    """Return storage usage in bytes for pdf, audio and total."""
    pdf_bytes = sum(
        os.path.getsize(os.path.join(PDF_DIR, f))
        for f in os.listdir(PDF_DIR)
        if os.path.isfile(os.path.join(PDF_DIR, f))
    )
    audio_bytes = sum(
        os.path.getsize(os.path.join(AUDIO_DIR, f))
        for f in os.listdir(AUDIO_DIR)
        if os.path.isfile(os.path.join(AUDIO_DIR, f))
    )
    total = pdf_bytes + audio_bytes
    return {
        "pdf_bytes": pdf_bytes,
        "audio_bytes": audio_bytes,
        "total_bytes": total,
        "quota_bytes": QUOTA_BYTES,
        "pdf_display": _fmt(pdf_bytes),
        "audio_display": _fmt(audio_bytes),
        "total_display": _fmt(total),
        "quota_display": _fmt(QUOTA_BYTES),
        "pct": min(round(total / QUOTA_BYTES * 100, 1), 100) if QUOTA_BYTES else 0,
    }


def _fmt(b: int) -> str:
    if b >= 1024**3:
        return f"{b / 1024**3:.1f} GB"
    if b >= 1024**2:
        return f"{b / 1024**2:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"


def check_quota(extra_bytes: int = 0) -> bool:
    """Return True if adding extra_bytes would exceed quota."""
    usage = get_storage_usage()
    return (usage["total_bytes"] + extra_bytes) > QUOTA_BYTES


def detect_file_type(filename: str) -> str | None:
    """Guess 'pdf' / 'audio' from the file extension, or None if unsupported."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return None


def part_name_from_filename(filename: str) -> str:
    """'03_Sopran-1.mp3' -> 'Sopran 1'"""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    stem = re.sub(r"^\d+[\s._-]+", "", stem)
    stem = re.sub(r"[_]+|(?<=\w)-(?=\w)", " ", stem)
    return stem.strip()[:255]


def safe_external_url(url: str) -> str | None:
    """Only allow http(s) links — blocks javascript: and data: URLs."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return url[:2000]
    return None


async def convert_audio_to_mp3(input_path: str) -> str:
    """Convert any audio file to MP3 44.1 kHz stereo using FFmpeg."""
    output_filename = f"{uuid.uuid4().hex}.mp3"
    output_path = os.path.join(AUDIO_DIR, output_filename)

    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vn",
        "-ar", "44100",
        "-ac", "2",
        "-b:a", "192k",
        "-y",
        output_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    if os.path.exists(input_path):
        os.remove(input_path)

    if process.returncode != 0:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise UploadError("Lydfilen kunne ikke konverteres — er det en gyldig lydfil?")

    return output_path


async def _stream_to_disk(upload_file, path: str) -> int:
    """Write an upload to disk in chunks, enforcing MAX_UPLOAD_BYTES. Returns size."""
    size = 0
    try:
        with open(path, "wb") as f:
            while chunk := await upload_file.read(_CHUNK):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise UploadError(f"Filen er for stor (max {_fmt(MAX_UPLOAD_BYTES)})")
                f.write(chunk)
    except BaseException:
        if os.path.exists(path):
            os.remove(path)
        raise
    return size


async def save_upload(upload_file, file_type: str) -> str:
    """Validate and save an uploaded file. Audio is converted to MP3 via FFmpeg."""
    if file_type not in ("pdf", "audio"):
        raise UploadError("Ukendt filtype")

    size_hint = getattr(upload_file, "size", None)
    if size_hint is not None and check_quota(size_hint):
        raise UploadError("Ikke nok plads — slet filer eller forhøj kvoten")

    if file_type == "pdf":
        if detect_file_type(upload_file.filename) != "pdf":
            raise UploadError("Kun .pdf-filer kan uploades som noder")
        file_path = os.path.join(PDF_DIR, f"{uuid.uuid4().hex}.pdf")
        await _stream_to_disk(upload_file, file_path)
        with open(file_path, "rb") as f:
            if not f.read(1024).lstrip().startswith(b"%PDF"):
                os.remove(file_path)
                raise UploadError(f'"{upload_file.filename}" er ikke en gyldig PDF')
        return file_path

    ext = os.path.splitext(upload_file.filename or "")[1].lower()
    if ext not in AUDIO_EXTENSIONS:
        raise UploadError(f"Lydformatet {ext or '(ukendt)'} understøttes ikke")
    temp_path = os.path.join(AUDIO_DIR, f"temp_{uuid.uuid4().hex}{ext}")
    await _stream_to_disk(upload_file, temp_path)
    return await convert_audio_to_mp3(temp_path)


def remove_stored_file(path: str) -> None:
    """Delete a stored upload, refusing anything outside the upload directory."""
    if _inside_uploads(path) and os.path.isfile(path):
        os.remove(path)


def _inside_uploads(path: str) -> bool:
    root = os.path.realpath(UPLOAD_DIR)
    return os.path.realpath(path).startswith(root + os.sep)


def content_disposition(kind: str, filename: str) -> str:
    """RFC 6266 header that survives æøå and quotes in filenames."""
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", filename) or "fil"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def download_name(original_filename: str, stored_path: str) -> str:
    """Original name, but with the extension of the stored file (audio becomes .mp3)."""
    stem = os.path.splitext(os.path.basename(original_filename or "fil"))[0] or "fil"
    return stem + os.path.splitext(stored_path)[1]


async def serve_file_with_range(file_path: str, request: Request, as_download: bool = False,
                                filename: str | None = None):
    """Serve a file with HTTP Range request support for audio seeking."""
    if not _inside_uploads(file_path) or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Fil ikke fundet")

    file_size = os.path.getsize(file_path)

    ext = os.path.splitext(file_path)[1].lower()
    content_types = {
        ".pdf": "application/pdf",
        ".mp3": "audio/mpeg",
    }
    content_type = content_types.get(ext, "application/octet-stream")
    filename = filename or os.path.basename(file_path)
    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
        "Content-Disposition": content_disposition("attachment" if as_download else "inline", filename),
    }

    range_header = request.headers.get("range")

    if range_header and not as_download:
        range_match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if range_match and (range_match.group(1) or range_match.group(2)):
            if range_match.group(1):
                start = int(range_match.group(1))
                # Open-ended ranges are served in bounded chunks to keep memory flat
                end = int(range_match.group(2)) if range_match.group(2) else start + 8 * _CHUNK - 1
            else:  # suffix range: last N bytes
                start = max(0, file_size - int(range_match.group(2)))
                end = file_size - 1

            if start >= file_size or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

            end = min(end, file_size - 1)
            content_length = end - start + 1

            with open(file_path, "rb") as f:
                f.seek(start)
                data = f.read(content_length)

            return Response(
                content=data,
                status_code=206,
                headers={
                    **base_headers,
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(content_length),
                    "Content-Type": content_type,
                },
            )

    return FileResponse(file_path, media_type=content_type, headers=base_headers)
