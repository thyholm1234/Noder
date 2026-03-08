import os
import re
import uuid
import asyncio
from fastapi import Request, HTTPException
from starlette.responses import Response, FileResponse

from app.config import QUOTA_BYTES

UPLOAD_DIR = "uploads"
PDF_DIR = os.path.join(UPLOAD_DIR, "pdf")
AUDIO_DIR = os.path.join(UPLOAD_DIR, "audio")

os.makedirs(PDF_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".wma", ".aiff", ".opus"}


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


async def convert_audio_to_mp3(input_path: str) -> str:
    """Convert any audio file to MP3 44.1 kHz stereo using FFmpeg."""
    output_filename = f"{uuid.uuid4().hex}.mp3"
    output_path = os.path.join(AUDIO_DIR, output_filename)

    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", input_path,
        "-ar", "44100",
        "-ac", "2",
        "-b:a", "192k",
        "-y",
        output_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    # Remove temp input file
    if os.path.exists(input_path):
        os.remove(input_path)

    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg fejl: {stderr.decode()}")

    return output_path


async def save_upload(upload_file, file_type: str) -> str:
    """Save an uploaded file. For audio, convert to MP3 via FFmpeg."""
    if file_type == "pdf":
        filename = f"{uuid.uuid4().hex}.pdf"
        file_path = os.path.join(PDF_DIR, filename)
        content = await upload_file.read()
        with open(file_path, "wb") as f:
            f.write(content)
        return file_path

    # Audio: save temp file, then convert
    ext = os.path.splitext(upload_file.filename or "")[1].lower()
    temp_filename = f"temp_{uuid.uuid4().hex}{ext}"
    temp_path = os.path.join(AUDIO_DIR, temp_filename)
    content = await upload_file.read()
    with open(temp_path, "wb") as f:
        f.write(content)

    return await convert_audio_to_mp3(temp_path)


async def serve_file_with_range(file_path: str, request: Request, as_download: bool = False):
    """Serve a file with HTTP Range request support for audio seeking."""
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Fil ikke fundet")

    file_size = os.path.getsize(file_path)

    # Determine content type
    ext = os.path.splitext(file_path)[1].lower()
    content_types = {
        ".pdf": "application/pdf",
        ".mp3": "audio/mpeg",
    }
    content_type = content_types.get(ext, "application/octet-stream")

    range_header = request.headers.get("range")

    if range_header and not as_download:
        range_match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2)) if range_match.group(2) else file_size - 1

            if start >= file_size:
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
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(content_length),
                    "Content-Type": content_type,
                },
            )

    # Full file response
    headers = {"Accept-Ranges": "bytes"}
    if as_download:
        original_name = os.path.basename(file_path)
        headers["Content-Disposition"] = f'attachment; filename="{original_name}"'

    return FileResponse(file_path, media_type=content_type, headers=headers)
