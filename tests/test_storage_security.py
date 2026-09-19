# P7 security hardening: unit-level tests for app/core/storage.py's
# defenses -- safe-identifier validation, path-containment enforcement
# (independent of the identifier regex, simulating a future caller that
# bypasses it), upload size limits, and file-type allowlisting. HTTP-layer
# regression tests for the same concerns (traversal-style ids rejected by
# the API, oversized/unsupported uploads rejected end to end) live in
# tests/test_onboarding_api.py.
import io

import pytest

from app.core.storage import (
    CAMERA_REFERENCE_ALLOWED_CONTENT_TYPES,
    CAMERA_REFERENCE_ALLOWED_EXTENSIONS,
    MAP_ALLOWED_CONTENT_TYPES,
    MAP_ALLOWED_EXTENSIONS,
    PROJECT_ROOT,
    VIDEO_ALLOWED_CONTENT_TYPES,
    VIDEO_ALLOWED_EXTENSIONS,
    UnsafeIdentifierError,
    UnsupportedFileTypeError,
    UploadTooLargeError,
    ensure_safe_identifier,
    save_upload,
)


class _FakeUpload:
    """Minimal duck-typed stand-in for fastapi.UploadFile -- save_upload
    only ever reads .filename, .content_type, and .file.read(n)."""

    def __init__(self, filename: str, content_type: str, data: bytes):
        self.filename = filename
        self.content_type = content_type
        self.file = io.BytesIO(data)


# ----------------------------------------------------------------------
# ensure_safe_identifier
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "../../etc/passwd",
        "..\\..\\windows",
        "a/b",
        "a\\b",
        "..",
        ".",
        "",
        " ST1001",
        "ST1001 ",
        "ST1001\x00",
        "-ST1001",  # must start with alphanumeric, not a separator char
        "a" * 65,  # over the 64-char cap
    ],
)
def test_ensure_safe_identifier_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(UnsafeIdentifierError):
        ensure_safe_identifier(value)


@pytest.mark.parametrize(
    "value",
    ["ST1001", "ST1002", "ST1001_MAIN_ZONE", "ST1001_CAM_ZONE_1", "ST-ELECTRONICS-9001", "a", "A1"],
)
def test_ensure_safe_identifier_accepts_existing_style_ids(value: str) -> None:
    assert ensure_safe_identifier(value) == value


# ----------------------------------------------------------------------
# save_upload: path containment
# ----------------------------------------------------------------------
def test_save_upload_rejects_traversal_subdir(tmp_path) -> None:
    base_dir = tmp_path / "maps"
    upload = _FakeUpload("floorplan.png", "image/png", b"\x89PNG\r\n\x1a\nbytes")

    with pytest.raises(UnsafeIdentifierError):
        save_upload(
            upload,
            base_dir,
            subdir="../../escape",
            max_bytes=1_000_000,
            allowed_extensions=MAP_ALLOWED_EXTENSIONS,
            allowed_content_types=MAP_ALLOWED_CONTENT_TYPES,
        )

    # Nothing should have been written anywhere, in base_dir or outside it.
    escaped_target = (base_dir / "../../escape").resolve()
    assert not escaped_target.exists()
    assert not base_dir.exists() or not any(base_dir.rglob("*"))


def test_save_upload_containment_check_blocks_escape_even_if_identifier_check_is_bypassed(
    tmp_path, monkeypatch
) -> None:
    """Simulates a future caller that bypasses ensure_safe_identifier (e.g. a
    bug, or code that doesn't go through the validated schema layer) --
    the pathlib resolve()/relative_to() containment check inside save_upload
    must independently stop the escape, per the P7 security-review
    requirement that storage.py not rely solely on the regex."""
    import app.core.storage as storage_module

    monkeypatch.setattr(storage_module, "ensure_safe_identifier", lambda value: value)

    base_dir = tmp_path / "maps"
    upload = _FakeUpload("floorplan.png", "image/png", b"\x89PNG\r\n\x1a\nbytes")

    with pytest.raises(UnsafeIdentifierError):
        storage_module.save_upload(
            upload,
            base_dir,
            subdir="../../escape",
            max_bytes=1_000_000,
            allowed_extensions=MAP_ALLOWED_EXTENSIONS,
            allowed_content_types=MAP_ALLOWED_CONTENT_TYPES,
        )

    escaped_target = (base_dir / "../../escape").resolve()
    assert not escaped_target.exists()


def test_save_upload_rejects_absolute_path_subdir(tmp_path) -> None:
    """Path's ``/`` operator discards the left operand entirely when the
    right operand is an absolute path (e.g. Path("/a")/"/etc" == Path("/etc"))
    -- this must be caught by the same containment check, not silently
    accepted."""
    base_dir = tmp_path / "maps"
    upload = _FakeUpload("floorplan.png", "image/png", b"\x89PNG\r\n\x1a\nbytes")

    absolute_subdir = str(PROJECT_ROOT.anchor) + "totally-elsewhere"
    with pytest.raises(UnsafeIdentifierError):
        save_upload(
            upload,
            base_dir,
            subdir=absolute_subdir,
            max_bytes=1_000_000,
            allowed_extensions=MAP_ALLOWED_EXTENSIONS,
            allowed_content_types=MAP_ALLOWED_CONTENT_TYPES,
        )


def test_save_upload_accepts_valid_subdir_and_writes_inside_base(tmp_path, monkeypatch) -> None:
    import app.core.storage as storage_module

    # save_upload's returned relative_path is always relative to the real
    # PROJECT_ROOT (app/core/storage.py callers only ever pass MAPS_DIR/
    # CAMERA_REFS_DIR, both real subdirs of it) -- point it at tmp_path here
    # so this isolated unit test can use a throwaway base_dir instead.
    monkeypatch.setattr(storage_module, "PROJECT_ROOT", tmp_path)

    base_dir = tmp_path / "maps"
    upload = _FakeUpload("floorplan.png", "image/png", b"\x89PNG\r\n\x1a\nbytes")

    absolute_path, relative_path = save_upload(
        upload,
        base_dir,
        subdir="ST1001",
        max_bytes=1_000_000,
        allowed_extensions=MAP_ALLOWED_EXTENSIONS,
        allowed_content_types=MAP_ALLOWED_CONTENT_TYPES,
    )

    assert absolute_path.is_file()
    assert absolute_path.read_bytes() == b"\x89PNG\r\n\x1a\nbytes"
    assert str(base_dir.resolve()) in str(absolute_path.resolve())
    assert not relative_path.startswith("..")


# ----------------------------------------------------------------------
# save_upload: size limit
# ----------------------------------------------------------------------
def test_save_upload_rejects_oversized_file_and_cleans_up(tmp_path) -> None:
    base_dir = tmp_path / "maps"
    upload = _FakeUpload("floorplan.png", "image/png", b"\x89PNG\r\n\x1a\n" + (b"x" * 1000))

    with pytest.raises(UploadTooLargeError):
        save_upload(
            upload,
            base_dir,
            subdir="ST1001",
            max_bytes=10,
            allowed_extensions=MAP_ALLOWED_EXTENSIONS,
            allowed_content_types=MAP_ALLOWED_CONTENT_TYPES,
        )

    # No partial file should be left behind.
    assert not any(base_dir.rglob("*.png")) if base_dir.exists() else True


def test_save_upload_accepts_file_within_size_limit(tmp_path, monkeypatch) -> None:
    import app.core.storage as storage_module

    monkeypatch.setattr(storage_module, "PROJECT_ROOT", tmp_path)

    base_dir = tmp_path / "maps"
    upload = _FakeUpload("floorplan.png", "image/png", b"\x89PNG\r\n\x1a\nbytes")

    absolute_path, _ = save_upload(
        upload,
        base_dir,
        subdir="ST1001",
        max_bytes=1_000_000,
        allowed_extensions=MAP_ALLOWED_EXTENSIONS,
        allowed_content_types=MAP_ALLOWED_CONTENT_TYPES,
    )
    assert absolute_path.is_file()


# ----------------------------------------------------------------------
# save_upload: file-type allowlist
# ----------------------------------------------------------------------
def test_save_upload_rejects_unsupported_type_for_map(tmp_path) -> None:
    base_dir = tmp_path / "maps"
    upload = _FakeUpload("payload.exe", "application/octet-stream", b"MZ...")

    with pytest.raises(UnsupportedFileTypeError):
        save_upload(
            upload,
            base_dir,
            subdir="ST1001",
            max_bytes=1_000_000,
            allowed_extensions=MAP_ALLOWED_EXTENSIONS,
            allowed_content_types=MAP_ALLOWED_CONTENT_TYPES,
        )


def test_save_upload_rejects_svg_for_camera_reference(tmp_path) -> None:
    """SVG is deliberately excluded even though it's a common raster-image
    substitute -- it's XML and can embed <script>."""
    base_dir = tmp_path / "camera_refs"
    upload = _FakeUpload("frame.svg", "image/svg+xml", b"<svg><script>alert(1)</script></svg>")

    with pytest.raises(UnsupportedFileTypeError):
        save_upload(
            upload,
            base_dir,
            subdir="ST1001",
            max_bytes=1_000_000,
            allowed_extensions=CAMERA_REFERENCE_ALLOWED_EXTENSIONS,
            allowed_content_types=CAMERA_REFERENCE_ALLOWED_CONTENT_TYPES,
        )


def test_save_upload_accepts_pdf_for_map_but_not_camera_reference(tmp_path, monkeypatch) -> None:
    import app.core.storage as storage_module

    monkeypatch.setattr(storage_module, "PROJECT_ROOT", tmp_path)

    pdf_upload = _FakeUpload("floorplan.pdf", "application/pdf", b"%PDF-1.4 fake")

    absolute_path, _ = save_upload(
        pdf_upload,
        tmp_path / "maps",
        subdir="ST1001",
        max_bytes=1_000_000,
        allowed_extensions=MAP_ALLOWED_EXTENSIONS,
        allowed_content_types=MAP_ALLOWED_CONTENT_TYPES,
    )
    assert absolute_path.is_file()

    with pytest.raises(UnsupportedFileTypeError):
        save_upload(
            _FakeUpload("floorplan.pdf", "application/pdf", b"%PDF-1.4 fake"),
            tmp_path / "camera_refs",
            subdir="ST1001",
            max_bytes=1_000_000,
            allowed_extensions=CAMERA_REFERENCE_ALLOWED_EXTENSIONS,
            allowed_content_types=CAMERA_REFERENCE_ALLOWED_CONTENT_TYPES,
        )


# ----------------------------------------------------------------------
# resolve_video_path: containment (P9)
#
# Camera.video_path is settable two ways: via save_upload (upload_camera_video,
# already contained by construction -- a fresh uuid4 filename under
# VIDEOS_DIR/<store_id>/) and as a plain string via the ADMIN-gated camera
# JSON config endpoints (create_camera/update_camera), which bypasses
# save_upload entirely. resolve_video_path is the independent containment
# check for the latter -- see its docstring.
# ----------------------------------------------------------------------
def test_resolve_video_path_accepts_a_path_inside_videos_dir(tmp_path, monkeypatch) -> None:
    import app.core.storage as storage_module

    monkeypatch.setattr(storage_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(storage_module, "VIDEOS_DIR", tmp_path / "data" / "videos")

    resolved = storage_module.resolve_video_path("data/videos/ST1001/abc123.mp4")
    assert resolved == (tmp_path / "data" / "videos" / "ST1001" / "abc123.mp4").resolve()


def test_resolve_video_path_rejects_dot_dot_traversal(tmp_path, monkeypatch) -> None:
    import app.core.storage as storage_module

    monkeypatch.setattr(storage_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(storage_module, "VIDEOS_DIR", tmp_path / "data" / "videos")

    with pytest.raises(UnsafeIdentifierError):
        storage_module.resolve_video_path("data/videos/../../outside/evil.mp4")


def test_resolve_video_path_rejects_absolute_path(tmp_path, monkeypatch) -> None:
    """Mirrors test_save_upload_rejects_absolute_path_subdir's reasoning:
    Path's ``/`` operator discards the left operand entirely when the right
    operand is absolute, so this must be caught by real containment, not a
    string-prefix check."""
    import app.core.storage as storage_module

    monkeypatch.setattr(storage_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(storage_module, "VIDEOS_DIR", tmp_path / "data" / "videos")

    absolute_elsewhere = str(PROJECT_ROOT.anchor) + "totally-elsewhere/video.mp4"
    with pytest.raises(UnsafeIdentifierError):
        storage_module.resolve_video_path(absolute_elsewhere)


def test_resolve_video_path_rejects_path_resolving_outside_videos_dir(tmp_path, monkeypatch) -> None:
    """A path with no literal ".." can still resolve outside VIDEOS_DIR --
    e.g. a sibling directory that shares a string prefix with it. Must be
    caught by resolve()+relative_to(), not a string-prefix comparison."""
    import app.core.storage as storage_module

    monkeypatch.setattr(storage_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(storage_module, "VIDEOS_DIR", tmp_path / "data" / "videos")

    with pytest.raises(UnsafeIdentifierError):
        storage_module.resolve_video_path("data/videos_backup/ST1001/abc123.mp4")


def test_resolve_video_path_accepts_the_exact_shape_save_upload_produces(tmp_path, monkeypatch) -> None:
    """The realistic case: a value save_upload actually produced (and the
    upload endpoint continues to work unchanged) must validate cleanly."""
    import app.core.storage as storage_module

    monkeypatch.setattr(storage_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(storage_module, "VIDEOS_DIR", tmp_path / "data" / "videos")

    upload = _FakeUpload("clip.mp4", "video/mp4", b"fake video bytes")
    _, relative_path = save_upload(
        upload,
        storage_module.VIDEOS_DIR,
        subdir="ST1001",
        max_bytes=1_000_000,
        allowed_extensions=VIDEO_ALLOWED_EXTENSIONS,
        allowed_content_types=VIDEO_ALLOWED_CONTENT_TYPES,
    )

    resolved = storage_module.resolve_video_path(relative_path)
    assert resolved.is_file()
    assert resolved.read_bytes() == b"fake video bytes"
