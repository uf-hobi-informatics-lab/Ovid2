"""Contract: an address is the *exact* location of its data.

Ovid2 is a processor — a command plus an input address and an output
address. Every address (disc folder, object-store prefix, warehouse
``DATABASE/SCHEMA``) is the exact location the data is read from or
written to; neither ``pipeline`` nor ``batch_id`` is ever appended as a
path segment (ADR:
the adr-address-is-exact-location design note).

This test walks the driver data-plane modules and fails if any of them
re-introduces the pre-2026-10 layouts:

- ``{root}/{pipeline}/{batch_id}`` — joining ``pipeline`` and
  ``batch_id`` into a persistent read / write path;
- ``OVID_{pipeline}_{batch_id}`` — the synthetic Snowflake schema.

Transient staging names (instance-scoped ``@~`` stage dirs, temporary
stage / table identifiers, the Databricks bounce sub-prefix) are
allow-listed: they are scratch space owned by one run, never the
operator-visible location.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: ``(relative file, expression fragment)`` pairs that are transient scratch
#: names not covered by the ``instance_id`` rule. Keep empty if possible.
_ALLOWED_FRAGMENTS: tuple[tuple[str, str], ...] = ()


def _data_plane_files(src_root: Path) -> list[Path]:
    """Return every driver / stager module that resolves storage paths."""
    files = sorted((src_root / "drivers").rglob("*.py"))
    files += sorted((src_root / "core" / "engines").rglob("*.py"))
    files.append(src_root / "cli" / "tools.py")
    return [f for f in files if "__pycache__" not in f.parts]


_PIPELINE_NAMES: frozenset[str] = frozenset({"pipeline", "pipeline_name"})
_BATCH_NAMES: frozenset[str] = frozenset({"batch", "batch_id"})


def _names_in(node: ast.AST) -> set[str]:
    """Return identifier / attribute names used anywhere inside ``node``."""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
    return out


def _path_builders(tree: ast.AST) -> list[tuple[int, ast.AST]]:
    """Return outermost path-building expressions (f-string, ``/``/``+``, join, format)."""
    candidates: list[tuple[int, ast.AST]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.JoinedStr)
            or (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div | ast.Add))
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"join", "format", "joinpath"}
            )
        ):
            candidates.append((node.lineno, node))
    return candidates


def path_layout_offenders(path: Path, rel: str) -> list[str]:
    """Return ``rel:line: expr`` for expressions mixing pipeline + batch (or ``OVID_`` + pipeline)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    seen: set[int] = set()
    for lineno, node in _path_builders(tree):
        names = _names_in(node)
        text = ast.unparse(node)
        mixes = bool(names & _PIPELINE_NAMES) and bool(names & _BATCH_NAMES)
        # Run-scoped scratch names (temporary stages / tables / bounce
        # prefixes) carry ``instance_id`` — they are never the operator's
        # location (see the module docstring).
        if mixes and "instance_id" in names:
            continue
        synthetic_schema = "OVID_" in text and bool(names & _PIPELINE_NAMES)
        if not (mixes or synthetic_schema) or lineno in seen:
            continue
        if any(rel == f and frag in text for f, frag in _ALLOWED_FRAGMENTS):
            continue
        seen.add(lineno)
        out.append(f"{rel}:{lineno}: {text[:120]}")
    return out


@pytest.mark.contract
def test_no_pipeline_batch_path_segments(src_root: Path) -> None:
    """No driver composes ``pipeline`` + ``batch_id`` into a storage path.

    AST-based: any f-string, ``/`` or ``+`` expression, or ``.join`` /
    ``.format`` / ``.joinpath`` call that combines a pipeline name with a
    batch name (or builds ``OVID_…`` from a pipeline) is flagged — the
    regex predecessor missed ``f"{root}/{pipeline}/{batch_id}"`` and
    ``"/".join([...])``.
    """
    offenders: list[str] = []
    for path in _data_plane_files(src_root):
        rel = path.relative_to(src_root).as_posix()
        offenders += path_layout_offenders(path, rel)
    assert not offenders, (
        "Drivers must treat the address as the exact location — no "
        "{pipeline}/{batch_id} segments or OVID_{pipeline}_{batch} schemas "
        "(see adr-address-is-exact-location.md):\n" + "\n".join(offenders)
    )


@pytest.mark.contract
def test_layout_detector_catches_evasions(tmp_path: Path) -> None:
    """Negative control: the spellings the regex version missed."""
    py = tmp_path / "d.py"
    py.write_text(
        'a = f"{root}/{pipeline}/{batch_id}"\n'
        'b = "/".join([root, pipeline, batch_id])\n'
        "c = Path(root) / pipeline / batch_id\n"
        'd = "OVID_" + pipeline\n'
        'e = "{}/{}/{}".format(root, self.pipeline, self.batch_id)\n'
        'ok = f"{root}/{table}"\n'
        'scratch = f"{instance_id}_{pipeline}_{batch_id}_{table}"\n',
        encoding="utf-8",
    )
    found = path_layout_offenders(py, "d.py")
    assert len(found) == 5, found


@pytest.mark.contract
def test_snowflake_has_no_synthetic_schema() -> None:
    """``SnowflakeAddress`` must not expose the removed ``_schema_name``."""
    from ovid2.drivers.snowflake.spark import SnowflakeAddress

    assert not hasattr(SnowflakeAddress, "_schema_name")


@pytest.mark.contract
def test_disc_resolver_signature_is_exact() -> None:
    """``resolve_read_path`` takes ``(root, pattern)`` only — no pipeline/batch."""
    import inspect

    from ovid2.drivers.disc.spark import resolve_read_path

    assert list(inspect.signature(resolve_read_path).parameters) == ["root_path", "file_pattern"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
