# P9: static checks that the video-processing worker is actually wired into
# the default Docker Compose stack. This project has no Docker-in-CI job (CI
# runs pytest/ruff directly against a Postgres *service container*, not
# docker-compose.yml itself -- see .github/workflows/ci.yml), so these tests
# parse docker-compose.yml/Dockerfile.worker as text/YAML rather than
# actually building or running containers. See the P9 completion audit's
# worker and deployment sections (3 and 9): previously there was no `worker`
# service at all, so a VideoProcessingJob created via the API would sit
# PENDING forever under `docker compose up`.
from __future__ import annotations

from pathlib import Path

import yaml  # transitive dependency of uvicorn[standard] (see requirements.txt) -- not a new dependency

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _compose() -> dict:
    return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def dockerfile_logical_lines(path: Path) -> list[str]:
    """Read a Dockerfile and collapse `\\`-continued RUN commands into a
    single logical line each, the same way a shell would join them before
    execution -- several tests below need to find a flag/filename that a
    human-readable Dockerfile wraps across multiple physical lines."""
    return path.read_text(encoding="utf-8").replace("\\\n", " ").splitlines()


def test_worker_service_exists_in_the_default_compose_stack() -> None:
    """The literal gap the P9 audit found: `docker compose up` (no profile
    flags) must start a worker, not just db/migrate/api."""
    compose = _compose()
    assert "worker" in compose["services"]
    assert "profiles" not in compose["services"]["worker"], (
        "worker must be in the default stack, not gated behind an opt-in Compose profile"
    )


def test_worker_builds_from_its_own_dockerfile_not_the_api_image() -> None:
    compose = _compose()
    worker = compose["services"]["worker"]
    api = compose["services"]["api"]
    assert worker["build"]["dockerfile"] == "Dockerfile.worker"
    assert worker["image"] != api["image"]


def test_worker_shares_video_storage_with_api() -> None:
    """Same named volume, mounted at the same path in both services --
    otherwise a file app/api/onboarding.py's upload_camera_video saves in the
    api container would not exist in the worker container that later opens
    it via pipeline.video.tracking.UltralyticsByteTracker."""
    compose = _compose()
    api_volumes = compose["services"]["api"].get("volumes") or []
    worker_volumes = compose["services"]["worker"].get("volumes") or []
    assert any(v.endswith(":/app/data/videos") for v in api_volumes)
    assert any(v.endswith(":/app/data/videos") for v in worker_volumes)

    api_volume_name = next(v.split(":")[0] for v in api_volumes if v.endswith(":/app/data/videos"))
    worker_volume_name = next(v.split(":")[0] for v in worker_volumes if v.endswith(":/app/data/videos"))
    assert api_volume_name == worker_volume_name
    assert api_volume_name in compose["volumes"]


def test_worker_receives_the_same_database_and_environment_config_as_api() -> None:
    compose = _compose()
    api_env = compose["services"]["api"]["environment"]
    worker_env = compose["services"]["worker"]["environment"]
    assert worker_env["DATABASE_URL"] == api_env["DATABASE_URL"]
    assert worker_env["ENVIRONMENT"] == api_env["ENVIRONMENT"]
    # Settings.get_settings() raises at startup for a non-development
    # environment left on the dev-only JWT placeholder (see
    # app/core/config.py) -- the worker calls get_settings() too (see
    # create_worker_engine's caller in run_video_worker.py), so it needs a
    # real value here just as api/migrate already do.
    assert worker_env["JWT_SECRET_KEY"]
    assert "insecure" not in worker_env["JWT_SECRET_KEY"].lower()


def test_worker_waits_for_migrations_before_starting() -> None:
    compose = _compose()
    depends_on = compose["services"]["worker"]["depends_on"]
    assert depends_on["db"]["condition"] == "service_healthy"
    assert depends_on["migrate"]["condition"] == "service_completed_successfully"


def test_worker_restarts_on_crash_but_is_not_scaled_by_default() -> None:
    """Restart policy covers the whole-process-died case; VideoProcessingWorker
    itself already isolates a single bad job (see
    app/worker/video_processing_worker.py's _process_job) so run_forever()
    keeps going without the container needing to restart at all in that case.
    No `deploy.replicas`/scale here -- multi-instance worker coordination is
    explicitly out of scope for this phase."""
    compose = _compose()
    worker = compose["services"]["worker"]
    assert worker.get("restart") == "unless-stopped"
    assert "deploy" not in worker


def test_worker_dockerfile_installs_the_video_stack_and_runs_the_worker_module() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    assert "requirements-video.txt" in dockerfile
    assert "app.worker.run_video_worker" in dockerfile


def test_worker_dockerfile_copies_requirements_txt_before_installing_video_requirements() -> None:
    """Regression guard for a real build failure caught by the P9 Docker
    smoke test: requirements-video.txt itself does `-r requirements.txt`
    (see that file), so both files must already be in the build context by
    the time `pip install -r requirements-video.txt` runs -- copying only
    requirements-video.txt makes pip fail with "Could not open requirements
    file: ... requirements.txt" before the later `COPY . .` ever brings it
    in."""
    # Joined so a `RUN` command split across `\`-continued lines (see
    # test_worker_dockerfile_installs_are_resilient_to_slow_downloads below)
    # is still matched as one logical line, the same way a shell reads it.
    lines = dockerfile_logical_lines(PROJECT_ROOT / "Dockerfile.worker")
    copy_line_index = next(
        i for i, line in enumerate(lines) if line.strip().startswith("COPY") and "requirements" in line
    )
    install_line_index = next(
        i for i, line in enumerate(lines) if "pip install" in line and "requirements-video.txt" in line
    )
    copy_line = lines[copy_line_index]

    assert "requirements.txt" in copy_line
    assert "requirements-video.txt" in copy_line
    assert copy_line_index < install_line_index


def test_worker_dockerfile_installs_are_resilient_to_slow_downloads() -> None:
    """Regression guard for a real build failure caught by the P9 Docker
    smoke test: torch/opencv are tens-of-MB wheels, and pip's 15s default
    per-read timeout was hit mid-download on a slow link, aborting the whole
    build with urllib3.ReadTimeoutError."""
    lines = dockerfile_logical_lines(PROJECT_ROOT / "Dockerfile.worker")
    install_line = next(line for line in lines if "pip install" in line and "requirements-video.txt" in line)
    assert "--timeout" in install_line
    assert "--retries" in install_line


def test_worker_dockerfile_prefers_cpu_only_torch_over_the_bundled_cuda_build() -> None:
    """Dependency-footprint guard: nothing in docker-compose.yml requests GPU
    access for `worker`, so installing ultralytics' `torch` dependency from
    PyPI's default index (which bundles a full CUDA toolkit, several GB of
    nvidia-cu*/triton packages the container can never use) would be a large,
    pointless image-size and build-time cost -- see the P9 Docker smoke
    test's build log."""
    dockerfile = (PROJECT_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    assert "download.pytorch.org/whl/cpu" in dockerfile


def test_worker_dockerfile_installs_the_system_libraries_opencv_needs_to_import() -> None:
    """Regression guard for a real container crash caught by the P9 Docker
    smoke test: opencv-python-headless still dynamically links a small
    handful of X11/GL client libraries at `import cv2` time despite dropping
    the actual GUI/display backend -- python:3.11-slim ships none of them, so
    the worker crash-looped one missing library at a time (first
    `libxcb.so.1`, then `libGL.so.1`) until this apt-get step was added."""
    dockerfile = (PROJECT_ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    assert "libxcb1" in dockerfile
    assert "libgl1" in dockerfile
    assert "libglib2.0-0" in dockerfile


def test_api_dockerfile_stays_lightweight_and_unaware_of_the_video_stack() -> None:
    """Regression guard for the audit's explicit constraint: the api image
    must keep installing only the bare requirements.txt. If this ever starts
    matching requirements-video.txt, the whole point of splitting the worker
    into its own image (keeping ultralytics/opencv/torch off every
    request-serving replica) is silently undone."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements.txt" in dockerfile
    assert "requirements-video" not in dockerfile


def test_video_uploads_volume_is_declared() -> None:
    compose = _compose()
    assert "video_uploads" in compose["volumes"]
