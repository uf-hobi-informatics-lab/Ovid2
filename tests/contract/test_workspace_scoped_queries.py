"""Contract: workspace isolation holds at the storage seams (adr-workspaces.md).

* **SQL.** Every ``SELECT`` / ``UPDATE`` / ``DELETE`` on a workspace-scoped
  row table (``ovid2.web.db.WORKSPACE_SCOPED_TABLES``) in the web services
  either filters on ``workspace_id`` or is listed in
  :data:`_CROSS_WORKSPACE` with the reason it may span workspaces (the
  worker's queue, a row already fetched through a scoped lookup, …). A new
  unscoped query fails the build.
* **Paths.** No module outside :mod:`ovid2.workspaces` / ``config.paths``
  builds a ``connections`` / ``configs`` / ``engines`` / ``llms`` /
  ``pipelines`` / ``workflows`` / ``transforms`` directory from the XDG
  root by hand — they must go through ``ovid2.workspaces.search_dirs`` /
  ``write_dir`` so the active workspace applies.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "ovid2"
_SERVICES = _SRC / "web" / "services"

#: ``(file, table)`` pairs whose statements may legitimately span
#: workspaces, with the reason. Statement-level: every statement on that
#: table in that file is exempt, so keep the list short and specific.
_CROSS_WORKSPACE: dict[tuple[str, str], str] = {
    # Worker queue: claim / pid / result / terminal / crash recovery act on
    # a job id the worker already owns, whatever its workspace.
    ("jobs_svc.py", "web_jobs"): (
        "row-level statements run on ids fetched through the scoped _fetchrow "
        "or by the worker (claim_next_queued / mark_* / resume_in_flight)"
    ),
    # Runner internals: steps / run updates by run id after a scoped _run_row.
    ("workflow_runner_svc.py", "web_workflow_runs"): (
        "_update_run / advance_all operate on ids from the scoped _run_row or "
        "iterate every active run inside use_workspace(run.workspace_id)"
    ),
    # Session rows are owner-scoped by _owned() before any update.
    ("llm_sessions_svc.py", "web_llm_sessions"): (
        "updates follow _owned(), which filters user_id AND workspace_id"
    ),
    # Report rows: every mutation follows the scoped _load().
    ("reports_svc.py", "web_reports"): "mutations follow the workspace-filtered _load()",
    # Workspace lifecycle: emptiness check by explicit workspace id.
    ("workspaces_svc.py", "web_jobs"): "workspace_is_empty() filters by an explicit id",
    ("workspaces_svc.py", "web_workflow_runs"): "workspace_is_empty() filters by an explicit id",
    ("workspaces_svc.py", "web_reports"): "workspace_is_empty() filters by an explicit id",
    # The list WHERE clause is the module constant ``_LIST_WHERE``, which
    # starts with ``workspace_id = ?`` (asserted below).
    ("report_sources_svc.py", "web_report_sources"): (
        "list_sources concatenates _LIST_WHERE, which begins with workspace_id"
    ),
}

_STMT_RE = re.compile(r"\b(SELECT|UPDATE|DELETE)\b", re.IGNORECASE)


def _sql_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, text)`` of every string literal (joined implicit concatenations)."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
    return out


def _scoped_tables() -> tuple[str, ...]:
    from ovid2.web.db import WORKSPACE_SCOPED_TABLES

    return WORKSPACE_SCOPED_TABLES


def _statements(path: Path) -> list[tuple[int, str]]:
    """Return SQL-looking string literals, joining ``"a " "b"`` / ``+`` chains per call."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in {"execute", "executemany"}):
            continue
        parts = [s for _ln, s in _sql_strings(node.args[0])]
        text = " ".join(parts)
        if _STMT_RE.search(text):
            found.append((node.lineno, text))
    return found


@pytest.mark.contract
def test_scoped_tables_are_filtered_by_workspace() -> None:
    """Every read / write of a scoped table filters on ``workspace_id`` (or is waived)."""
    tables = _scoped_tables()
    problems: list[str] = []
    for path in sorted(_SERVICES.glob("*.py")):
        for lineno, sql in _statements(path):
            low = sql.lower()
            if low.lstrip().startswith("insert"):
                continue
            for table in tables:
                if not re.search(r"\b" + table + r"\b", low):
                    continue
                if "workspace_id" in low:
                    continue
                if (path.name, table) in _CROSS_WORKSPACE:
                    continue
                problems.append(f"{path.name}:{lineno}: {table}: {sql.strip()[:100]}")
    assert not problems, (
        "Queries on workspace-scoped tables must filter on workspace_id "
        "(or be waived in _CROSS_WORKSPACE with a reason):\n" + "\n".join(problems)
    )


@pytest.mark.contract
def test_inserts_stamp_workspace() -> None:
    """Every ``INSERT`` into a scoped table writes the ``workspace_id`` column."""
    tables = _scoped_tables()
    problems: list[str] = []
    for path in sorted(_SERVICES.glob("*.py")):
        for lineno, sql in _statements(path):
            low = sql.lower()
            if "insert" not in low:
                continue
            for table in tables:
                if re.search(r"into\s+" + table + r"\b", low) and "workspace_id" not in low:
                    problems.append(
                        f"{path.name}:{lineno}: INSERT INTO {table} without workspace_id"
                    )
    assert not problems, "\n".join(problems)


@pytest.mark.contract
def test_report_sources_list_where_is_scoped() -> None:
    """The waived report-source list clause really filters on the workspace."""
    from ovid2.web.services import report_sources_svc

    assert report_sources_svc._LIST_WHERE.startswith("workspace_id = ?")


@pytest.mark.contract
def test_waivers_are_still_needed() -> None:
    """Every waiver names a file that still touches that table (no stale waivers)."""
    stale = []
    for (name, table), reason in _CROSS_WORKSPACE.items():
        assert len(reason.split()) >= 3, f"waiver {name}/{table} needs a real reason"
        path = _SERVICES / name
        if not path.is_file() or table not in path.read_text(encoding="utf-8"):
            stale.append(f"{name}/{table}")
    assert not stale, f"stale _CROSS_WORKSPACE waivers: {stale}"


_KIND_DIRS = ("connections", "configs", "engines", "llms", "pipelines", "workflows", "transforms")

#: Modules allowed to compose ``<xdg>/ovid2/<kind>`` paths directly.
_PATH_ALLOW: frozenset[str] = frozenset(
    {
        "workspaces.py",
        "config/paths.py",
        # dev sandboxes create their own private XDG tree.
        "cli/dev.py",
        # installer seeds the default (flat) tree on first install.
        "installer/init.py",
        "installer/doctor.py",
    }
)

_XDG_JOIN = re.compile(
    r"""(["']ovid2["']\s*\)?\s*/\s*["'](?P<kind>"""
    + "|".join(_KIND_DIRS)
    + r""")["'])|(config_dir\(\)\s*/\s*["'](?P<kind2>"""
    + "|".join(_KIND_DIRS)
    + r""")["'])"""
)


@pytest.mark.contract
def test_kind_dirs_go_through_workspaces() -> None:
    """Config-kind directories are resolved via ``ovid2.workspaces`` only."""
    problems: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _PATH_ALLOW or rel.startswith(("legacy/", "tools/legacy")):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _XDG_JOIN.search(line):
                problems.append(f"{rel}:{lineno}: {line.strip()[:100]}")
    assert not problems, (
        "Build config-kind directories with ovid2.workspaces.search_dirs / write_dir "
        "so the active workspace applies (adr-workspaces.md):\n" + "\n".join(problems)
    )


@pytest.mark.contract
def test_negative_control_unscoped_query_is_caught(tmp_path: Path) -> None:
    """A fabricated unscoped query is reported by the statement scanner."""
    bad = tmp_path / "bad_svc.py"
    bad.write_text(
        "def f(conn):\n    conn.execute('SELECT * FROM web_jobs WHERE job_id = ?', (1,))\n",
        encoding="utf-8",
    )
    stmts = _statements(bad)
    assert stmts and "workspace_id" not in stmts[0][1].lower()
