from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import UploadFile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
MAPS_DIR = DATA_DIR / "maps"
CAMERA_REFS_DIR = DATA_DIR / "camera_refs"
VIDEOS_DIR = DATA_DIR / "videos"

# P7: small, dependency-free local file storage for onboarding uploads (store
# maps, camera reference stills). Deliberately not object storage/S3 -- the
# rest of the app already reads/writes local files under data/ at runtime
# (see docs/CHOICES.md's "Dependency Footprint" entry and .dockerignore's
# notes on data/), and nothing about map/reference-image storage needs more
# than that for this phase.


class StorageError(ValueError):
    """Base class for storage-layer validation failures. A ValueError
    subclass so it composes with the existing ValueError->400 convention
    used elsewhere in this codebase (see app.api.stores._run_ranged and
    app.api.onboarding._run), while still being catchable specifically."""


class UnsafeIdentifierError(StorageError):
    """Raised when an identifier that is about to be used as a filesystem
    path segment (e.g. a store id, used as an upload subdirectory) does not
    match the safe-identifier format. This is deliberately checked again
    here, not just at the Pydantic schema boundary (see
    app.schemas.spatial's SAFE_IDENTIFIER_PATTERN) -- a caller that
    constructs a path from an id without going through the validated API
    schemas (a future script, a different route, a bug) must not be able to
    escape the storage root."""


class UploadTooLargeError(StorageError):
    """Raised when an upload exceeds its configured maximum size. Detected
    during streaming, not after buffering the whole file -- see
    save_upload's chunked read loop."""


class UnsupportedFileTypeError(StorageError):
    """Raised when an upload's extension or declared content type is not on
    the allowlist for what it's being uploaded as (map vs. camera reference
    image)."""


# Identifiers (store_id, zone_id, camera_id) are used as SQL primary keys
# everywhere, and store_id is additionally used as a filesystem directory
# name for uploads (see save_upload below). This pattern is deliberately
# conservative -- alphanumeric plus underscore/hyphen, starting with an
# alphanumeric character, 1-64 characters -- which accepts every id already
# in use (e.g. "ST1001", "ST1001_BILLING_QUEUE", "ST1001_CAM_ZONE_1") while
# rejecting path separators, "..", null bytes, and leading/trailing
# whitespace outright.
SAFE_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_SAFE_IDENTIFIER_RE = re.compile(SAFE_IDENTIFIER_PATTERN)

# Raster image types only -- SVG is deliberately excluded even though it's a
# common "image" format: it's XML and can embed <script>, which would reopen
# a stored-content risk for a file served back from this app's own origin
# (see docs/CHOICES.md's P7 security-hardening entry).
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_IMAGE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

MAP_ALLOWED_EXTENSIONS = _IMAGE_EXTENSIONS | {".pdf"}
MAP_ALLOWED_CONTENT_TYPES = _IMAGE_CONTENT_TYPES | {"application/pdf"}
CAMERA_REFERENCE_ALLOWED_EXTENSIONS = _IMAGE_EXTENSIONS
CAMERA_REFERENCE_ALLOWED_CONTENT_TYPES = _IMAGE_CONTENT_TYPES

# P9: recorded CCTV footage uploaded to a Camera for processing. Container
# formats OpenCV/Ultralytics already read directly (see
# pipeline/video/tracking.py) -- no transcoding is performed, so the
# allowlist is deliberately narrow rather than "accept anything video/*".
VIDEO_ALLOWED_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv"})
VIDEO_ALLOWED_CONTENT_TYPES = frozenset(
    {"video/mp4", "video/quicktime", "video/x-msvideo", "video/x-matroska"}
)


def ensure_safe_identifier(value: str) -> str:
    """Raise UnsafeIdentifierError unless ``value`` is a safe identifier.
    Returns ``value`` unchanged so this can be used inline."""
    if not _SAFE_IDENTIFIER_RE.match(value):
        raise UnsafeIdentifierError(
            f"{value!r} is not a valid identifier "
            f"(expected to match {SAFE_IDENTIFIER_PATTERN!r})."
        )
    return value


def _validate_upload_type(upload: UploadFile, *, allowed_extensions: frozenset[str], allowed_content_types: frozenset[str]) -> None:
    suffix = Path(upload.filename or "").suffix.lower()
    content_type = (upload.content_type or "").lower()
    if suffix not in allowed_extensions or content_type not in allowed_content_types:
        raise UnsupportedFileTypeError(
            f"Unsupported upload: filename suffix {suffix!r}, content type {content_type!r}. "
            f"Allowed extensions: {sorted(allowed_extensions)}; allowed content types: {sorted(allowed_content_types)}."
        )


def save_upload(
    upload: UploadFile,
    directory: Path,
    *,
    subdir: str,
    max_bytes: int,
    allowed_extensions: frozenset[str],
    allowed_content_types: frozenset[str],
) -> tuple[Path, str]:
    """Persist an uploaded file under ``directory/subdir/<uuid><ext>``.

    Defends against three things regardless of whether the caller already
    validated its input: ``subdir`` escaping ``directory`` (path traversal),
    an oversized upload (streamed and aborted mid-write, never buffered
    whole), and a disallowed file type. Returns (absolute_path,
    path_relative_to_project_root) -- the relative path is what gets stored
    in the database, so the storage root can move without invalidating
    persisted rows.
    """
    ensure_safe_identifier(subdir)
    _validate_upload_type(upload, allowed_extensions=allowed_extensions, allowed_content_types=allowed_content_types)

    directory_resolved = directory.resolve()
    target_dir = (directory / subdir).resolve()
    try:
        target_dir.relative_to(directory_resolved)
    except ValueError:
        # Defense in depth: ensure_safe_identifier above should already have
        # rejected anything that could reach this, but a resolved-path
        # containment check is the actual guarantee, not the regex.
        raise UnsafeIdentifierError(f"{subdir!r} would escape the storage root.") from None

    target_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(upload.filename or "").suffix
    filename = f"{uuid.uuid4()}{suffix}"
    absolute_path = target_dir / filename

    total_bytes = 0
    try:
        with absolute_path.open("wb") as out_file:
            while chunk := upload.file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise UploadTooLargeError(
                        f"Upload exceeds the maximum allowed size of {max_bytes} bytes."
                    )
                out_file.write(chunk)
    except UploadTooLargeError:
        absolute_path.unlink(missing_ok=True)
        raise

    relative_path = absolute_path.relative_to(PROJECT_ROOT)
    return absolute_path, str(relative_path).replace("\\", "/")


def resolve_relative_path(relative_path: str) -> Path:
    return PROJECT_ROOT / relative_path


def resolve_video_path(relative_path: str) -> Path:
    """Resolve a stored Camera.video_path (or VideoProcessingJob.video_path
    snapshot) to an absolute filesystem path, raising UnsafeIdentifierError
    unless it stays inside VIDEOS_DIR.

    save_upload's own output is already contained by construction (a fresh
    uuid4 filename under VIDEOS_DIR/<store_id>/ -- see save_upload above), so
    this is a no-op for it. It exists because Camera.video_path is *also*
    settable directly as a plain string via the ADMIN-gated camera-config
    JSON endpoints (app.api.onboarding's create_camera/update_camera),
    completely bypassing save_upload -- so a value that later gets opened as
    a real file (the P9 video-processing worker; see
    pipeline.video.config.build_config_for_job, which calls this too as
    defense-in-depth) cannot be trusted to be contained just because it came
    from the database. Rejects absolute paths (POSIX or Windows-drive) and
    ../ traversal the same way save_upload's own subdir check does: resolve
    both sides and require actual containment, not a string prefix match.
    """
    videos_dir_resolved = VIDEOS_DIR.resolve()
    candidate = (PROJECT_ROOT / relative_path).resolve()
    try:
        candidate.relative_to(videos_dir_resolved)
    except ValueError:
        raise UnsafeIdentifierError(
            f"{relative_path!r} does not resolve inside the video storage directory."
        ) from None
    return candidate
