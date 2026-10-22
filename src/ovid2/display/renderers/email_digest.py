"""Email-digest renderer.

Consumes an instance's event log and produces one HTML + plain-text
summary email per completed batch. The renderer is intentionally
stateless — it reads the finished log after the run, aggregates counts
and errors, and hands the message off to SMTP.

The transport itself is pluggable: production ships with
:class:`SmtpMailer`, tests inject an in-memory recorder. Credentials
come from ``~/.config/ovid2/secrets.d/smtp.json``.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

from ovid2.display.reader import read_events

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Summary aggregation
# ---------------------------------------------------------------------------


@dataclass
class BatchSummary:
    """Aggregated metrics for one run (a batch, or a health-check run).

    Ovid2 emails digests for both ETL runs (one *item* per CDM table)
    and data-health / ``dq_checks`` runs (one *item* per check). Health
    events carry neither ``pipeline`` nor ``batch_id``, so a run is keyed
    on ``(pipeline or instance_id, batch_id or instance_id)`` — see
    :func:`summarise`. A ``check.completed`` with ``check_status`` other
    than ``"passed"`` counts as a non-OK item (two-bucket ok/failed,
    matching the terminal renderers).
    """

    pipeline: str = ""
    batch_id: str = ""
    instance_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    tables_ok: list[str] = field(default_factory=list)
    tables_failed: list[tuple[str, str]] = field(default_factory=list)  # (table, error)
    checks_ok: list[str] = field(default_factory=list)
    checks_failed: list[tuple[str, str]] = field(default_factory=list)  # (check, reason)
    fixes_applied: int = 0
    fixes_failed: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    success: bool = True

    @property
    def tables_total(self) -> int:
        return len(self.tables_ok) + len(self.tables_failed)

    @property
    def checks_total(self) -> int:
        return len(self.checks_ok) + len(self.checks_failed)

    @property
    def is_health(self) -> bool:
        """True when this run reported checks rather than tables."""
        return self.checks_total > 0 and self.tables_total == 0


def summarise(records: Iterable[Mapping[str, Any]]) -> list[BatchSummary]:
    """Aggregate events into one :class:`BatchSummary` per run.

    ETL runs key naturally on ``(pipeline, batch_id)``. Health runs carry
    neither field on their ``check.*`` events, so the key falls back to
    the ``instance_id`` — this is what lets a health digest be produced at
    all (previously such events were dropped for lacking a ``batch_id``).

    ``instance.started`` / ``instance.finished`` events carry no batch
    context, so their metadata (start/finish time, overall success) is
    applied in a second pass to every summary sharing that ``instance_id``.
    """
    records = list(records)
    summaries: dict[tuple[str, str], BatchSummary] = {}

    def _get(pipeline: str, batch_id: str, instance_id: str) -> BatchSummary:
        key = (pipeline or instance_id, batch_id or instance_id)
        summary = summaries.get(key)
        if summary is None:
            summary = BatchSummary(pipeline=pipeline, batch_id=batch_id, instance_id=instance_id)
            summaries[key] = summary
        return summary

    # Pass 1: item / fix / diagnostic events establish real run context.
    for record in records:
        kind = record.get("kind", "")
        pipeline = str(record.get("pipeline") or "")
        batch_id = str(record.get("batch_id") or "")
        instance_id = str(record.get("instance_id") or "")

        if kind == "table.completed":
            _get(pipeline, batch_id, instance_id).tables_ok.append(
                str(record.get("table") or "")
            )
        elif kind == "table.failed":
            summary = _get(pipeline, batch_id, instance_id)
            summary.tables_failed.append(
                (str(record.get("table") or ""), str(record.get("error") or ""))
            )
            summary.success = False
        elif kind == "check.completed":
            summary = _get(pipeline, batch_id, instance_id)
            check = str(record.get("check_id") or "")
            status = str(record.get("check_status") or "")
            if status in {"exception", "error"}:
                summary.checks_failed.append((check, status))
                summary.success = False
            else:
                summary.checks_ok.append(check)
        elif kind == "check.failed":
            summary = _get(pipeline, batch_id, instance_id)
            summary.checks_failed.append(
                (str(record.get("check_id") or ""), str(record.get("error") or ""))
            )
            summary.success = False
        elif kind == "fix.applied":
            _get(pipeline, batch_id, instance_id).fixes_applied += 1
        elif kind == "fix.failed":
            _get(pipeline, batch_id, instance_id).fixes_failed += 1
        elif kind == "warning":
            _get(pipeline, batch_id, instance_id).warnings.append(
                str(record.get("message") or "")
            )
        elif kind == "error":
            _get(pipeline, batch_id, instance_id).errors.append(
                str(record.get("message") or "")
            )

    # Pass 2: attach instance-level metadata to every summary of the instance.
    for record in records:
        kind = record.get("kind", "")
        if kind not in {"instance.started", "instance.finished"}:
            continue
        instance_id = str(record.get("instance_id") or "")
        ts = str(record.get("ts") or "")
        targets = [s for s in summaries.values() if s.instance_id == instance_id]
        if not targets:
            # A run that emitted only instance.* events (no items). Surface it
            # as a single instance-keyed summary rather than dropping it.
            targets = [_get("", "", instance_id)]
        for summary in targets:
            if kind == "instance.started" and not summary.started_at:
                summary.started_at = ts
            elif kind == "instance.finished":
                summary.finished_at = ts
                extra = record.get("extra") or {}
                if isinstance(extra, Mapping) and not extra.get("success", True):
                    summary.success = False

    return list(summaries.values())


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------


def render_text(summary: BatchSummary) -> str:
    """Render a plain-text summary."""
    lines: list[str] = [
        f"Ovid2 batch: {summary.pipeline} / {summary.batch_id}",
        f"Instance:    {summary.instance_id}",
        f"Started:     {summary.started_at}",
        f"Finished:    {summary.finished_at}",
        f"Status:      {'SUCCESS' if summary.success else 'FAILURE'}",
        "",
    ]
    if summary.is_health:
        lines.append(
            f"Checks:      {len(summary.checks_ok)} ok, {len(summary.checks_failed)} failed"
        )
    else:
        lines.append(
            f"Tables:      {len(summary.tables_ok)} ok, {len(summary.tables_failed)} failed"
        )
        if summary.fixes_applied or summary.fixes_failed:
            lines.append(
                f"Fixes:       {summary.fixes_applied} applied, {summary.fixes_failed} failed"
            )
    if summary.tables_failed:
        lines.append("")
        lines.append("Failed tables:")
        for name, error in summary.tables_failed:
            lines.append(f"  - {name}: {error}")
    if summary.checks_failed:
        lines.append("")
        lines.append("Failed checks:")
        for name, reason in summary.checks_failed:
            lines.append(f"  - {name}: {reason}")
    if summary.errors:
        lines.append("")
        lines.append("Errors:")
        for msg in summary.errors[-20:]:
            lines.append(f"  - {msg}")
    if summary.warnings:
        lines.append("")
        lines.append("Warnings:")
        for msg in summary.warnings[-20:]:
            lines.append(f"  - {msg}")
    return "\n".join(lines) + "\n"


def render_html(summary: BatchSummary) -> str:
    """Render an HTML summary."""
    status_color = "#137333" if summary.success else "#c5221f"
    rows: list[str] = [
        f'<tr><th align="left">Pipeline</th><td>{summary.pipeline}</td></tr>',
        f'<tr><th align="left">Batch</th><td>{summary.batch_id}</td></tr>',
        f'<tr><th align="left">Instance</th><td>{summary.instance_id}</td></tr>',
        f'<tr><th align="left">Started</th><td>{summary.started_at}</td></tr>',
        f'<tr><th align="left">Finished</th><td>{summary.finished_at}</td></tr>',
        f'<tr><th align="left">Status</th>'
        f'<td style="color: {status_color}; font-weight: bold;">'
        f"{'SUCCESS' if summary.success else 'FAILURE'}</td></tr>",
    ]
    if summary.is_health:
        rows.append(
            f'<tr><th align="left">Checks</th>'
            f"<td>{len(summary.checks_ok)} ok, "
            f"{len(summary.checks_failed)} failed</td></tr>"
        )
    else:
        rows.append(
            f'<tr><th align="left">Tables</th>'
            f"<td>{len(summary.tables_ok)} ok, "
            f"{len(summary.tables_failed)} failed</td></tr>"
        )
        if summary.fixes_applied or summary.fixes_failed:
            rows.append(
                f'<tr><th align="left">Fixes</th>'
                f"<td>{summary.fixes_applied} applied, {summary.fixes_failed} failed</td></tr>"
            )

    failed_section = ""
    if summary.tables_failed:
        items = "".join(
            f"<li><code>{name}</code>: {error}</li>" for name, error in summary.tables_failed
        )
        failed_section = f"<h3>Failed tables</h3><ul>{items}</ul>"

    failed_checks_section = ""
    if summary.checks_failed:
        items = "".join(
            f"<li><code>{name}</code>: {reason}</li>" for name, reason in summary.checks_failed
        )
        failed_checks_section = f"<h3>Failed checks</h3><ul>{items}</ul>"

    errors_section = ""
    if summary.errors:
        items = "".join(f"<li>{msg}</li>" for msg in summary.errors[-20:])
        errors_section = f"<h3>Errors</h3><ul>{items}</ul>"

    return (
        "<html><body>"
        f"<h2>Ovid2 batch: {summary.pipeline} / {summary.batch_id}</h2>"
        f'<table cellpadding="4">{"".join(rows)}</table>'
        f"{failed_section}"
        f"{failed_checks_section}"
        f"{errors_section}"
        "</body></html>"
    )


def build_message(
    summary: BatchSummary,
    *,
    sender: str,
    recipients: list[str],
    subject_prefix: str = "[ovid2]",
) -> EmailMessage:
    """Return a :class:`EmailMessage` for a summary."""
    msg = EmailMessage()
    status = "SUCCESS" if summary.success else "FAILURE"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"{subject_prefix} {summary.pipeline}/{summary.batch_id} — {status}"
    msg.set_content(render_text(summary))
    msg.add_alternative(render_html(summary), subtype="html")
    return msg


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class Mailer(Protocol):
    """Anything that can send an :class:`EmailMessage`."""

    def send(self, message: EmailMessage) -> None: ...


@dataclass
class SmtpConfig:
    """Connection parameters loaded from ``~/.config/ovid2/secrets.d/smtp.json``."""

    host: str
    port: int = 25
    username: str | None = None
    password: str | None = None
    use_starttls: bool = True
    timeout_seconds: int = 30

    @classmethod
    def from_secret(cls) -> SmtpConfig:
        """Load an :class:`SmtpConfig` from the ``smtp`` secret file."""
        from ovid2.config.secrets import load_secret

        data = load_secret("smtp")
        return cls(
            host=str(data["host"]),
            port=int(data.get("port", 25)),
            username=data.get("username"),
            password=data.get("password"),
            use_starttls=bool(data.get("use_starttls", True)),
            timeout_seconds=int(data.get("timeout_seconds", 30)),
        )


class SmtpMailer:
    """Concrete :class:`Mailer` backed by :mod:`smtplib`."""

    def __init__(self, config: SmtpConfig) -> None:
        self.config = config

    def send(self, message: EmailMessage) -> None:
        """Deliver ``message`` over SMTP."""
        with smtplib.SMTP(
            self.config.host, self.config.port, timeout=self.config.timeout_seconds
        ) as smtp:
            if self.config.use_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self.config.username and self.config.password:
                smtp.login(self.config.username, self.config.password)
            smtp.send_message(message)


class RecordingMailer:
    """Test double that records sent messages instead of transmitting."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def dispatch_digest(
    event_log: Path,
    *,
    sender: str,
    recipients: list[str],
    mailer: Mailer,
    subject_prefix: str = "[ovid2]",
) -> int:
    """Read ``event_log``, summarise, and send one message per batch.

    Args:
        event_log: JSONL event log for a completed instance.
        sender: RFC 5322 ``From`` address.
        recipients: RFC 5322 ``To`` addresses.
        mailer: Transport implementation.
        subject_prefix: Prefix for every email subject line.

    Returns:
        Number of messages sent.
    """
    records = list(read_events(event_log))
    summaries = summarise(records)
    for summary in summaries:
        message = build_message(
            summary,
            sender=sender,
            recipients=recipients,
            subject_prefix=subject_prefix,
        )
        try:
            mailer.send(message)
        except Exception as exc:
            logger.error(
                "Failed to send digest for %s/%s: %s", summary.pipeline, summary.batch_id, exc
            )
    return len(summaries)
