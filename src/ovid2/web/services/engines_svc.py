"""Engine editor service (E1).

Owns the CRUD path for engine files under
``$XDG_CONFIG_HOME/ovid2/engines/``. Engines describe *how the pipeline
computes* — they wrap a Spark session, a Databricks-Connect cluster, or
a Snowpark session. They control compute resources (memory, cores,
warehouse size) only; they never influence application logic.

Rules enforced here (mirror :mod:`ovid2.web.services.connections_svc`):

- Every mutation lands in the user-scoped engines directory.
  System-wide files under ``/etc/ovid2/engines`` are read-only through
  the web tier.
- Secret material (Databricks token, Snowflake password / private key)
  is never persisted to the engine JSON. ``EngineWrite.secrets`` values
  are written to a mode-``0o600`` file via
  :mod:`ovid2.web.services.secrets_svc`, and the engine file records the
  secret name in ``secret_ref`` only.
- Sensitive values echoed back from the UI as the sentinel ``***`` are
  preserved from the existing on-disk secret.
- Every write appends a ``web_audit`` row with pre/post content hashes.
- ``delete_engine`` refuses to delete an engine currently referenced by
  any route or by ``settings.defaults.engine``.

The service reads engine metadata (field spec catalogue) from
:mod:`ovid2.config.engines`. It never imports pipeline code
(:mod:`ovid2.core.engines.*`) — the test probe is delegated to a
subprocess of ``ovid2 tools test-engine <name>`` so the web tier stays
pyspark-free. Dispatch is by engine ``type``:

- **``pyspark``** — probe runs inside
  :data:`ovid2.config.settings.DOCKER_IMAGE` via ``docker run --rm``.
  This tests the *exact* Spark + JDK stack the ETL will run against,
  and means the host does not need a JDK installed.
- **``databricks_spark`` / ``snowpark``** — probe runs in a host
  subprocess (``sys.executable -m ovid2 tools test-engine``). These
  engines only need a Python client library.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ovid2.config import engines as _engines
from ovid2.web import db as web_db
from ovid2.web.models import (
    EngineDetail,
    EngineFieldSpec,
    EngineSummary,
    EngineTestReport,
    EngineTypeSpec,
    EngineWrite,
    WriteResult,
)
from ovid2.web.services import secrets_svc

logger = logging.getLogger(__name__)

__all__ = [
    "SENTINEL_MASK",
    "EngineError",
    "delete_engine",
    "get_engine",
    "list_engine_types",
    "list_engines",
    "test_engine",
    "write_engine",
]

SENTINEL_MASK: str = "***"

_SAMPLE_SUFFIX: str = "_sample"


class EngineError(ValueError):
    """Raised when an engine payload is invalid or unsafe."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> None:
    """Reject filename stems that would escape the engines directory."""
    if not name:
        raise EngineError("Engine name is required.")
    if not all(c.isalnum() or c in "-_." for c in name):
        raise EngineError(f"Invalid engine name '{name}'. Use letters, digits, '-', '_', '.' only.")
    if name.startswith(".") or name.endswith(_SAMPLE_SUFFIX):
        raise EngineError(f"Engine name '{name}' is reserved.")


def _writable_path(name: str) -> Path:
    """Return the user-scoped write target for an engine JSON."""
    directory = _engines.engines_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{name}.json"


def _hash(payload: dict[str, Any]) -> str:
    """Return a stable content hash for audit rows."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _default_secret_ref(name: str) -> str:
    """Return the default secret-file stem for an engine with no ``secret_ref``."""
    return f"engine_{name}"


def _validate_args_against_spec(
    type_name: str, args: dict[str, Any], secrets: dict[str, Any]
) -> None:
    """Check that required fields are present and no field is misplaced."""
    spec = _engines.engine_field_spec(type_name)
    known_args = {f["name"] for f in spec["args"]}
    known_secrets = {f["name"] for f in spec["secrets"]}
    for f in spec["args"]:
        if f.get("required") and not args.get(f["name"]):
            raise EngineError(f"'{type_name}' engines require the '{f['name']}' argument.")
    misplaced = sorted(set(args) & known_secrets)
    if misplaced:
        raise EngineError(
            f"Fields {misplaced} are secrets for '{type_name}'. "
            "Move them into the 'secrets' payload."
        )
    unknown_args = sorted(set(args) - known_args)
    if unknown_args:
        raise EngineError(
            f"Unknown args {unknown_args} for '{type_name}'. Known: {sorted(known_args)}."
        )
    unknown_secrets = sorted(set(secrets) - known_secrets)
    if unknown_secrets:
        raise EngineError(
            f"Unknown secrets {unknown_secrets} for '{type_name}'. Known: {sorted(known_secrets)}."
        )


def _read_raw(path: Path) -> dict[str, Any]:
    """Return the raw on-disk contents of an engine file."""
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise EngineError(f"Engine file {path} is not a JSON object.")
    return data


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


def list_engines() -> list[EngineSummary]:
    """Return every engine config discoverable on the search path."""
    summaries: list[EngineSummary] = []
    for name in _engines.list_available_engines():
        path = _engines.locate_engine(name)
        if path is None:
            continue
        try:
            payload = _read_raw(path)
        except (OSError, json.JSONDecodeError, EngineError):
            continue
        summaries.append(
            EngineSummary(
                name=name,
                type=str(payload.get("type", "unknown")),
                path=str(path),
                secret_ref=(str(payload.get("secret_ref")) if payload.get("secret_ref") else None),
            )
        )
    return summaries


def get_engine(name: str) -> EngineDetail | None:
    """Return one engine with masked secret placeholders."""
    path = _engines.locate_engine(name)
    if path is None:
        return None
    try:
        payload = _read_raw(path)
    except (OSError, json.JSONDecodeError, EngineError):
        return None
    type_name = str(payload.get("type", "unknown"))
    secret_ref = payload.get("secret_ref") or None
    masked_secrets: dict[str, str] = {}
    if secret_ref:
        detail = secrets_svc.get_secret(str(secret_ref))
        if detail is not None:
            masked_secrets = dict.fromkeys(detail.keys, SENTINEL_MASK)
    return EngineDetail(
        name=name,
        type=type_name,
        path=str(path),
        args=dict(payload.get("args") or {}),
        secret_ref=str(secret_ref) if secret_ref else None,
        secrets=masked_secrets,
    )


def list_engine_types() -> list[EngineTypeSpec]:
    """Return the field-spec catalogue for every registered engine.

    Used by ``GET /api/v1/engines/types`` — the UI (and MCP tools)
    consume this to render the dynamic form.
    """
    specs: list[EngineTypeSpec] = []
    for type_name in _engines.list_engine_types():
        spec = _engines.engine_field_spec(type_name)
        specs.append(
            EngineTypeSpec(
                type=type_name,
                args=[EngineFieldSpec(**f) for f in spec["args"]],
                secrets=[EngineFieldSpec(**f) for f in spec["secrets"]],
            )
        )
    return specs


# ---------------------------------------------------------------------------
# Write / delete
# ---------------------------------------------------------------------------


def _merge_secret_updates(ref: str | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Preserve on-disk secret values for keys sent as the mask sentinel."""
    existing: dict[str, Any] = {}
    if ref:
        try:
            existing_detail = secrets_svc.get_secret(ref)
            if existing_detail is not None:
                candidate = Path(existing_detail.path)
                if candidate.is_file():
                    try:
                        existing = json.loads(candidate.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        existing = {}
        except Exception:  # pragma: no cover — defensive
            existing = {}
    merged: dict[str, Any] = {}
    for key, value in incoming.items():
        if value == SENTINEL_MASK:
            existing_value = existing.get(key)
            if existing_value in (None, ""):
                raise EngineError(
                    f"Secret field '{key}' is masked but has no value on disk. "
                    "Enter the real value before saving."
                )
            merged[key] = existing_value
        else:
            merged[key] = value
    return merged


def write_engine(
    name: str,
    payload: EngineWrite,
    *,
    actor: str = "web",
    create_only: bool = False,
) -> WriteResult:
    """Create or update an engine JSON.

    Secret material lands in a separate mode-0o600 secret file. Only
    the reference (``secret_ref``) is stored in the engine JSON.

    Args:
        name: Filename stem.
        payload: Parsed :class:`EngineWrite` from the client.
        actor: Free-form origin label recorded in ``web_audit``.
        create_only: When True, refuse to overwrite an existing file.

    Returns:
        A :class:`WriteResult` describing the write.

    Raises:
        EngineError: On validation failure or a create-conflict.
    """
    _validate_name(name)
    type_name = str(payload.type)
    if type_name not in _engines.list_engine_types():
        raise EngineError(
            f"Unknown engine type '{type_name}'. Registered: {_engines.list_engine_types()}."
        )

    incoming_args = dict(payload.args or {})
    incoming_secrets = dict(payload.secrets or {})
    _validate_args_against_spec(type_name, incoming_args, incoming_secrets)

    existing_path = _engines.locate_engine(name)
    existing_raw: dict[str, Any] | None = None
    if existing_path is not None:
        if create_only:
            raise EngineError(f"Engine '{name}' already exists.")
        try:
            existing_raw = _read_raw(existing_path)
        except (OSError, json.JSONDecodeError, EngineError) as exc:
            raise EngineError(f"Existing config is not readable: {exc}") from exc

    secret_ref: str | None = None
    if incoming_secrets:
        secret_ref = str(
            payload.secret_ref
            or (existing_raw or {}).get("secret_ref")
            or _default_secret_ref(name)
        )
        merged_secrets = _merge_secret_updates(secret_ref, incoming_secrets)
        from ovid2.web.models import SecretWrite  # local import to avoid loop

        try:
            secrets_svc.write_secret(
                secret_ref,
                SecretWrite(values=merged_secrets),
                actor=actor,
            )
        except secrets_svc.SecretError as exc:
            raise EngineError(f"Secret write failed: {exc}") from exc
    else:
        secret_ref = (
            str(payload.secret_ref)
            if payload.secret_ref
            else (
                str(existing_raw.get("secret_ref"))
                if existing_raw and existing_raw.get("secret_ref")
                else None
            )
        )

    doc: dict[str, Any] = {"type": type_name, "args": incoming_args}
    if secret_ref:
        doc["secret_ref"] = secret_ref

    target = _writable_path(name)
    before_hash = _hash(existing_raw) if existing_raw is not None else None
    after_hash = _hash(doc)
    action = "updated" if existing_raw is not None else "created"

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(target)

    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action=f"engine.{action}",
        target=name,
        before_hash=before_hash,
        after_hash=after_hash,
        detail_json=json.dumps({"path": str(target), "type": type_name}),
    )
    logger.info("Engine %s %s at %s", action, name, target)
    return WriteResult(name=name, path=str(target), action=action)


def _find_references(name: str) -> list[str]:
    """Return human-readable descriptions of every place ``name`` is referenced.

    Used by :func:`delete_engine` to refuse deletion of an engine that
    is currently in use. Scans pipelines and the ``settings.defaults.engine``
    field. Never scans queued/running jobs — those are transient.
    """
    references: list[str] = []

    # Pipelines.
    with contextlib.suppress(Exception):
        from ovid2.web.services import pipelines_svc

        for summary in pipelines_svc.list_pipelines():
            detail = pipelines_svc.get_pipeline(summary.name)
            if detail is not None and detail.engine == name:
                references.append(f"pipeline '{summary.name}'")

    # Settings default.
    with contextlib.suppress(Exception):
        from ovid2.web.services import settings_svc

        settings = settings_svc.get_settings()
        if settings.defaults.engine == name:
            references.append("settings.defaults.engine")

    return references


def delete_engine(name: str, *, actor: str = "web") -> WriteResult:
    """Delete an engine JSON (and audit the event).

    Refuses when the engine is referenced by any route or by
    ``settings.defaults.engine``. The referenced secret file is *not*
    removed automatically — deleting the secret is a separate
    ``DELETE /api/v1/secrets/{name}`` call.
    """
    _validate_name(name)
    target = _writable_path(name)
    if not target.is_file():
        raise EngineError(f"Engine '{name}' not found under {_engines.engines_dir()}.")

    refs = _find_references(name)
    if refs:
        raise EngineError(
            f"Engine '{name}' is still in use by: {', '.join(refs)}. "
            "Update or delete those references before deleting the engine."
        )

    before_hash: str | None = None
    try:
        before_hash = _hash(_read_raw(target))
    except (OSError, json.JSONDecodeError, EngineError):
        before_hash = None
    target.unlink()
    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action="engine.deleted",
        target=name,
        before_hash=before_hash,
        detail_json=json.dumps({"path": str(target)}),
    )
    logger.info("Engine deleted %s at %s", name, target)
    return WriteResult(name=name, path=str(target), action="deleted")


# ---------------------------------------------------------------------------
# Test probe (subprocess)
# ---------------------------------------------------------------------------


_TEST_TIMEOUT_SECONDS: float = 120.0
_IMAGE_INSPECT_TIMEOUT_SECONDS: float = 5.0


def _resolve_engine_type(name: str) -> str | None:
    """Best-effort lookup of an engine's ``type`` field.

    Returns the type name (``pyspark``, ``databricks_spark``,
    ``snowpark``) when the config resolves, or ``None`` if the name
    is unknown / the file is malformed. Callers dispatch on this to
    decide *where* to run the probe.

    Also honours the bare-type back-compat path — a caller who asks
    for ``"pyspark"`` without ever having written an engine file
    still gets ``"pyspark"`` back (matches
    :func:`ovid2.core.loaders.load_engine`'s resolution order).
    """
    path = _engines.locate_engine(name)
    if path is not None:
        try:
            payload = _read_raw(path)
        except (OSError, json.JSONDecodeError, EngineError):
            return None
        type_name = payload.get("type")
        if isinstance(type_name, str) and type_name:
            return type_name
        return None
    # Bare-type back-compat: `pyspark` resolves even without a file.
    if name in _engines.list_engine_types():
        return name
    return None


def _pyspark_probe_image() -> str | None:
    """Return the Docker image tag to use for the ``pyspark`` probe.

    Source of truth is :data:`ovid2.config.settings.DOCKER_IMAGE`
    (overridable via ``OVID2_DOCKER_IMAGE``). Returning ``None``
    signals "no image configured" — callers surface this as a clean
    ``no_image`` result rather than trying to guess.
    """
    from ovid2.config.settings import DOCKER_IMAGE

    tag = DOCKER_IMAGE.strip() if isinstance(DOCKER_IMAGE, str) else ""
    return tag or None


def _image_present_locally(image: str) -> tuple[bool, str]:
    """Ask Docker whether ``image`` is already pulled/built locally.

    Returns a ``(present, message)`` pair. ``present`` is ``False``
    both when Docker is not installed and when ``docker image
    inspect`` returns a non-zero exit. Message carries a short
    operator-facing explanation.
    """
    docker = shutil.which("docker")
    if docker is None:
        return False, "`docker` is not on PATH. Install Docker to enable the pyspark probe."
    try:
        completed = subprocess.run(
            [docker, "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=_IMAGE_INSPECT_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"Could not query Docker for image {image!r}: {exc}"
    if completed.returncode != 0:
        return (
            False,
            f"Docker image {image!r} is not built yet. "
            "Run `ovid2 image build` or use the /install wizard.",
        )
    return True, f"Docker image {image!r} is present."


def _pyspark_docker_argv(name: str, image: str) -> list[str]:
    """Assemble the ``docker run`` argv for the pyspark probe.

    - Read-only bind mounts the host's engines directory into ``/etc/ovid2/engines``
      so the containerised ``ovid2 tools test-engine <name>`` resolves the
      same config.
    - ``--network host`` so a pipeline ``master_url`` pointing at a
      local cluster still connects the same way ETL runs will.
    - ``XDG_CONFIG_HOME=/etc`` inside the container so the standard
      lookup path finds the mounted engines.

    The image's ``ENTRYPOINT`` is ``ovid2 …``, so the argv passed
    after ``<image>`` are already the *subcommand* arguments
    (``tools test-engine <name>``) — do not prepend another ``ovid2``.
    """
    xdg_home = (os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")).rstrip("/")
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "-e",
        "XDG_CONFIG_HOME=/etc",
        "-v",
        f"{xdg_home}/ovid2:/etc/ovid2:ro",
        image,
        "tools",
        "test-engine",
        name,
    ]


def _host_probe_argv(name: str) -> list[str]:
    """Assemble the host-side probe argv (databricks-connect / snowpark)."""
    return [sys.executable, "-m", "ovid2", "tools", "test-engine", name]


def _resolve_probe_argv(name: str) -> tuple[list[str], EngineTestReport | None]:
    """Pick the right probe argv for ``name`` or return an early ``no_image`` report.

    Dispatches on the engine's ``type``:

    - ``pyspark`` → run inside ``settings.DOCKER_IMAGE`` so the test
      exercises the exact JDK/Spark the ETL will use. Refuses when
      no image is configured or the image is not built locally.
    - Everything else (``databricks_spark``, ``snowpark``) → run in
      a host subprocess. Those engines only need Python + a client
      library; wrapping them in the Spark image is wasteful.
    - Unknown engine name → fall through to the host subprocess so
      it can emit the canonical ``config`` error itself.
    """
    type_name = _resolve_engine_type(name)
    if type_name == "pyspark":
        image = _pyspark_probe_image()
        if image is None:
            return [], EngineTestReport(
                ok=False,
                code="no_image",
                message=(
                    "No Ovid2 Docker image configured. Set "
                    "`OVID2_DOCKER_IMAGE` or accept the default tag, "
                    "then run `ovid2 image build`."
                ),
                detail={"hint": "The pyspark engine probes inside the ETL image."},
            )
        present, reason = _image_present_locally(image)
        if not present:
            return [], EngineTestReport(
                ok=False,
                code="no_image",
                message=reason,
                detail={
                    "image": image,
                    "hint": (
                        "Build it once with `ovid2 image build` (or the "
                        "/install wizard). The pyspark engine's Test button "
                        "runs inside this image, not on the host."
                    ),
                },
            )
        return _pyspark_docker_argv(name, image), None
    return _host_probe_argv(name), None


def test_engine(name: str) -> EngineTestReport:
    """Run the engine's cheap round-trip probe and return the result.

    Dispatch:

    - **``pyspark``** — runs inside :data:`ovid2.config.settings.DOCKER_IMAGE`
      via ``docker run --rm``. The image bundles Spark + a JDK, so
      the host does not need Java installed. If no image is
      configured or built, returns ``code="no_image"`` without
      launching anything.
    - **``databricks_spark`` / ``snowpark``** — run in a host
      subprocess (``sys.executable -m ovid2 tools test-engine``).
      Those engines only need a Python client library, not a JVM.

    In every case the probe subprocess speaks the JSON contract
    documented in :func:`ovid2.cli.tools._test_engine_entry` — one
    JSON object on stdout, one newline. We parse the last JSON line
    of stdout so extra logging on stderr / earlier stdout lines
    does not break the flow.
    """
    argv, early = _resolve_probe_argv(name)
    if early is not None:
        return early

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return EngineTestReport(
            ok=False,
            code="timeout",
            message=f"Engine probe for '{name}' timed out after {_TEST_TIMEOUT_SECONDS:.0f}s.",
            detail={"argv": argv},
        )
    except FileNotFoundError as exc:
        return EngineTestReport(
            ok=False,
            code="error",
            message=f"Could not launch engine probe: {exc}",
            detail={"argv": argv},
        )

    # Try to parse the last JSON line from stdout.
    stdout = (completed.stdout or "").strip()
    parsed: dict[str, Any] | None = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "ok" in candidate:
            parsed = candidate
            break

    if parsed is None:
        return EngineTestReport(
            ok=False,
            code="error",
            message=(
                "Engine probe subprocess did not return a JSON result. "
                f"Exit code: {completed.returncode}."
            ),
            detail={
                "stdout_tail": stdout[-2000:],
                "stderr_tail": (completed.stderr or "")[-2000:],
                "returncode": completed.returncode,
            },
        )

    return EngineTestReport(
        ok=bool(parsed.get("ok", False)),
        code=str(parsed.get("code", "error")),
        message=str(parsed.get("message", "")),
        detail=dict(parsed.get("detail") or {}),
    )
