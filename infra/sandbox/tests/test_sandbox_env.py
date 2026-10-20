"""Contract tests for the analysis container.

These parse `services/api/Dockerfile` and `infra/sandbox/run-hardened.sh` and
assert the security properties infra/sandbox/README.md claims. They need no
Docker, so they run in CI: the point is that a property cannot be dropped
silently, only deliberately, by also editing the test and the README.

What they cannot do is prove the built image behaves this way. That needs a
real build; `test_docker_available` still skips without Docker.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "services" / "api" / "Dockerfile"
RUN_SCRIPT = REPO_ROOT / "infra" / "sandbox" / "run-hardened.sh"
FLY_TOML = REPO_ROOT / "fly.toml"


def _strip_comments(text: str) -> str:
    """Comments in these files quote the very flags under test, so a naive
    substring search would pass on prose alone."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(("#", "`#"))
    )


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return _strip_comments(DOCKERFILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def run_script() -> str:
    return _strip_comments(RUN_SCRIPT.read_text(encoding="utf-8"))


# ------------------------------------------------------- image contract


def test_runtime_stage_runs_as_non_root(dockerfile: str) -> None:
    """The process reads untrusted repository content (CLAUDE.md #4)."""
    users = re.findall(r"^USER\s+(\S+)", dockerfile, re.MULTILINE)
    assert users, "no USER directive: the image would run as root"
    assert all(u not in {"root", "0", "0:0"} for u in users), users
    # A numeric uid, so the property holds even if the account is renamed.
    assert users[-1].split(":")[0].isdigit(), users[-1]


def test_image_is_multi_stage_and_runtime_has_no_uv(dockerfile: str) -> None:
    """uv and its cache belong to the builder; the runtime image ships neither."""
    stages = re.findall(r"^FROM\s+\S+(?:\s+AS\s+(\S+))?", dockerfile, re.MULTILINE)
    assert len(stages) >= 2, "expected a builder stage and a runtime stage"
    runtime = dockerfile.split("FROM python:3.12-slim-bookworm AS runtime", 1)[-1]
    assert "uv sync" not in runtime
    assert "/usr/local/bin/uv" not in runtime


def test_dependencies_come_from_the_lockfile_only(dockerfile: str) -> None:
    """`uv sync --frozen` or nothing: no loose versions may enter the image."""
    assert "uv sync --frozen" in dockerfile
    syncs = re.findall(r"uv sync[^\n\\]*", dockerfile)
    assert syncs, "no uv sync found"
    for sync in syncs:
        assert "--frozen" in sync, sync
        assert "--no-dev" in sync, f"test/lint tooling would ship: {sync}"
    assert not re.search(r"\bpip install\b", dockerfile), "pip install bypasses uv.lock"


def test_python_is_312(dockerfile: str) -> None:
    """uv.lock pins requires-python == 3.12.*; a mismatch fails at sync time,
    but far more usefully here."""
    assert re.search(r"^FROM python:3\.12-", dockerfile, re.MULTILINE)


def test_git_is_present_because_two_modules_shell_out_to_it(dockerfile: str) -> None:
    """app/repository/ingest.py and app/analysis/scan/history.py need it."""
    installs = re.findall(r"apt-get install[^\n&]*", dockerfile)
    assert installs, "nothing installed: git would be missing"
    assert any(re.search(r"\bgit\b", line) for line in installs), installs


@pytest.mark.parametrize(
    "forbidden",
    [
        "gcc",
        "g++",
        "clang",
        "build-essential",
        "make",
        "cmake",
        "nodejs",
        "npm",
        "yarn",
        "git-lfs",
        "curl",
        "wget",
    ],
)
def test_no_toolchain_an_analyzed_repo_could_invoke(dockerfile: str, forbidden: str) -> None:
    """CLAUDE.md non-negotiable #3: never execute imported code. The strongest
    form of that is leaving nothing in the image able to build or fetch
    anything."""
    installs = " ".join(re.findall(r"apt-get install[^\n&]*", dockerfile))
    assert not re.search(rf"(?<![\w-]){re.escape(forbidden)}(?![\w-])", installs), (
        f"{forbidden} is installed in the image"
    )


def test_setuid_bits_are_stripped(dockerfile: str) -> None:
    """No in-image path back to uid 0 for a non-root process."""
    assert re.search(r"find\s+/\s+-xdev.*-perm\s+/6000", dockerfile)


def test_application_code_is_not_writable_by_the_app_user(dockerfile: str) -> None:
    """A read-only rootfs is decorative if the code is owned by the runtime
    uid, so every COPY into the runtime stage must land root-owned."""
    runtime = dockerfile.split("AS runtime", 1)[-1]
    copies = re.findall(r"^COPY\s+(.*)$", runtime, re.MULTILINE)
    assert copies, "runtime stage copies nothing"
    for copy in copies:
        assert "--chown=root:root" in copy, copy


def test_bytecode_writes_are_disabled(dockerfile: str) -> None:
    """The rootfs is read-only where the platform allows it; .pyc writes into
    code we want immutable are noise at best."""
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile


def test_logs_go_to_stdout_not_a_file(dockerfile: str) -> None:
    """app/logging.py logs structured lines to stdout; the image must not
    buffer them or redirect them anywhere."""
    assert "PYTHONUNBUFFERED=1" in dockerfile
    assert not re.search(r">>?\s*/var/log", dockerfile)


def test_scratch_is_tmpdir_so_checkout_disposal_still_recognises_it(dockerfile: str) -> None:
    """ingest.is_scratch_checkout only deletes a checkout that is a direct
    child of tempfile.gettempdir(). If TMPDIR moves, checkouts leak."""
    assert "TMPDIR=/tmp" in dockerfile


def test_entrypoint_serves_the_real_asgi_app(dockerfile: str) -> None:
    entrypoint = re.search(r"ENTRYPOINT\s+\[(.*?)\]", dockerfile, re.DOTALL)
    assert entrypoint, "no ENTRYPOINT"
    body = entrypoint.group(1).replace("\\\n", " ")
    assert '"uvicorn"' in body
    assert '"app.main:app"' in body
    # A scan runs inside the ASGI call via BackgroundTasks, so a short
    # graceful-shutdown window kills jobs on every deploy.
    assert "--timeout-graceful-shutdown" in body


# ------------------------------------------------- self-hosted run contract


@pytest.mark.parametrize(
    "flag",
    [
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit",
        "--user 10001:10001",
    ],
)
def test_hardened_run_script_keeps_its_flags(run_script: str, flag: str) -> None:
    assert flag in run_script, f"{flag} missing from infra/sandbox/run-hardened.sh"


def test_scratch_tmpfs_is_noexec(run_script: str) -> None:
    """Untrusted repository content is checked out here. noexec is the single
    most important flag in the script."""
    tmpfs = re.search(r"--tmpfs\s+/tmp:(\S+)", run_script)
    assert tmpfs, "no tmpfs mount for /tmp"
    opts = set(tmpfs.group(1).split(","))
    assert {"noexec", "nosuid", "nodev"} <= opts, opts


# ------------------------------------------------------------ fly contract


def test_fly_does_not_suspend_a_machine_mid_scan() -> None:
    """Jobs run in-process after the response is sent, so an autostopping
    machine looks idle exactly while it is scanning, and nothing reaps the
    orphaned job row."""
    fly = _strip_comments(FLY_TOML.read_text(encoding="utf-8"))
    assert re.search(r'auto_stop_machines\s*=\s*"off"', fly)
    assert re.search(r"min_machines_running\s*=\s*[1-9]", fly)


def test_fly_health_check_targets_the_unauthenticated_route() -> None:
    """/health is the only route in app/main.py that does not require a Clerk
    JWT; a check against anything else would fail closed."""
    fly = _strip_comments(FLY_TOML.read_text(encoding="utf-8"))
    assert re.search(r'path\s*=\s*"/health"', fly)


def test_fly_bakes_no_secrets() -> None:
    """Credentials come from `fly secrets`. Anything secret-shaped in [env]
    would be committed to git and shipped in the deployment manifest."""
    fly = FLY_TOML.read_text(encoding="utf-8")
    env_block = fly.split("[env]", 1)[-1].split("[http_service]", 1)[0]
    assigned = re.findall(r"^\s*([A-Z_]+)\s*=", env_block, re.MULTILINE)
    secretish = ("KEY", "SECRET", "TOKEN", "PASSWORD", "DATABASE_URL")
    assert not [name for name in assigned if any(s in name for s in secretish)], assigned


# ----------------------------------------------------- needs real Docker


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker not installed")
def test_docker_available() -> None:
    """The contract tests above are static. Proving the built image behaves as
    documented needs a real build; see docs/deployment.md."""
    assert shutil.which("docker")
