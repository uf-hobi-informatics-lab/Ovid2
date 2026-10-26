"""HTML pages for LLM connections + assistant routing (L1).

Left-menu entry **Configure → LLMs**:

- ``GET  /llms``                — list + routing card (general / System 1)
- ``GET  /llms/new?provider=``  — new-connection form (fields from the spec)
- ``POST /llms``                — create
- ``GET  /llms/{name}``         — detail (masked key) + test
- ``GET  /llms/{name}/edit``    — edit form
- ``POST /llms/{name}``         — update
- ``POST /llms/{name}/test``    — brokered probe, re-render detail
- ``POST /llms/{name}/delete``  — delete (refused while routed to)
- ``POST /llms/routing``        — save ``settings.llm``

Every route requires ``manage_config``; every POST checks CSRF.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ovid2.config import llms as _llms
from ovid2.web.auth.csrf import require_csrf
from ovid2.web.auth.deps import require_capability
from ovid2.web.auth.roles import Capability
from ovid2.web.models import LlmWrite, SettingsLlm
from ovid2.web.services import llms_svc, settings_svc
from ovid2.web.templating import templates

router = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_capability(Capability.MANAGE_CONFIG))],
)


def _split_form(form: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    args: dict[str, str] = {}
    secrets: dict[str, str] = {}
    for key, value in form.items():
        if key.startswith("arg_") and value.strip() != "":
            args[key[4:]] = value.strip() if "\n" not in value else value
        elif key.startswith("sec_") and value.strip() != "":
            secrets[key[4:]] = value.strip()
    return args, secrets


def _err(url: str, exc: Exception) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url=f"{url}{sep}error={quote(str(exc))}", status_code=303)


@router.get("/llms", response_class=HTMLResponse)
def llms_page(request: Request, error: str | None = None, flash: str | None = None) -> HTMLResponse:
    """List LLM connections and the routing card."""
    llms = llms_svc.list_llms()
    return templates.TemplateResponse(
        request,
        "llms.html",
        {
            "llms": llms,
            "general_choices": [x for x in llms if x.role == "general"] or llms,
            "system1_choices": [x for x in llms if x.role == "system1"],
            "routing": settings_svc.get_settings().llm,
            "error": error,
            "flash": flash,
        },
    )


@router.post("/llms/routing", response_class=HTMLResponse)
async def llms_routing_post(
    request: Request, _csrf: None = Depends(require_csrf)
) -> RedirectResponse:
    """Persist ``settings.llm`` from the routing card."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    current = settings_svc.get_settings()
    known = {x.name for x in llms_svc.list_llms()}
    try:
        general = form.get("general", "").strip() or None
        system1 = form.get("system1", "").strip() or None
        for label, value in (("general", general), ("system1", system1)):
            if value and value not in known:
                raise ValueError(f"{label} references unknown LLM connection '{value}'.")
        if general and system1 and general == system1:
            raise ValueError("General and System 1 must be different connections.")
        llm = SettingsLlm(
            enabled=bool(form.get("enabled")),
            general=general,
            system1=system1,
            confidence_threshold=float(form.get("confidence_threshold") or 0.7),
            fallback_to_general=bool(form.get("fallback_to_general")),
            max_concurrent_chats=int(form.get("max_concurrent_chats") or 4),
            chat_timeout_seconds=int(form.get("chat_timeout_seconds") or 180),
            tools_enabled=bool(form.get("tools_enabled")),
            max_tool_iterations=int(
                form.get("max_tool_iterations") or current.llm.max_tool_iterations
            ),
            tool_result_max_chars=int(
                form.get("tool_result_max_chars") or current.llm.tool_result_max_chars
            ),
        )
        if llm.enabled and not (llm.general or llm.system1):
            raise ValueError("Pick at least a general LLM before enabling the assistant.")
        settings_svc.save_settings(current.model_copy(update={"llm": llm}), actor="web")
    except (ValueError, settings_svc.SettingsError) as exc:
        return _err("/llms", exc)
    return RedirectResponse(url="/llms?flash=Routing+saved", status_code=303)


@router.get("/llms/new", response_class=HTMLResponse)
def llm_new_page(
    request: Request, provider: str = "openai_compatible", role: str = "", error: str | None = None
) -> HTMLResponse:
    """Render the new-connection form."""
    providers = _llms.list_providers()
    if provider not in providers:
        provider = "openai_compatible"
    spec = _llms.llm_field_spec(provider)
    if role not in spec["roles"]:
        role = spec["roles"][0]
    defaults = {
        f["name"]: f["default"] for f in spec["args"] if f.get("required") and f.get("default")
    }
    return templates.TemplateResponse(
        request,
        "llm_edit.html",
        {
            "mode": "new",
            "name": "",
            "provider": provider,
            "role": role,
            "providers": providers,
            "spec": spec,
            "args": defaults,
            "secrets": {},
            "error": error,
        },
    )


@router.post("/llms", response_class=HTMLResponse)
async def llm_create_post(
    request: Request, _csrf: None = Depends(require_csrf)
) -> RedirectResponse:
    """Create an LLM connection from the form."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    name = form.get("name", "").strip()
    provider = form.get("provider", "openai_compatible")
    role = form.get("role", "general")
    args, secrets = _split_form(form)
    try:
        llms_svc.write_llm(
            name,
            LlmWrite(provider=provider, role=role, args=args, secrets=secrets, secret_ref=None),
            actor="web",
            create_only=True,
        )
    except (llms_svc.LlmError, ValueError) as exc:
        return _err(f"/llms/new?provider={quote(provider)}&role={quote(role)}", exc)
    return RedirectResponse(url=f"/llms/{quote(name)}?flash=Saved", status_code=303)


@router.get("/llms/{name}/edit", response_class=HTMLResponse)
def llm_edit_page(request: Request, name: str, error: str | None = None) -> HTMLResponse:
    """Render the edit form."""
    detail = llms_svc.get_llm(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"LLM connection '{name}' not found.")
    return templates.TemplateResponse(
        request,
        "llm_edit.html",
        {
            "mode": "edit",
            "name": name,
            "provider": detail.provider,
            "role": detail.role,
            "providers": _llms.list_providers(),
            "spec": _llms.llm_field_spec(detail.provider),
            "args": detail.args,
            "secrets": detail.secrets,
            "error": error,
        },
    )


@router.post("/llms/{name}/test", response_class=HTMLResponse)
def llm_test_post(request: Request, name: str, _csrf: None = Depends(require_csrf)) -> HTMLResponse:
    """Probe and re-render the detail page."""
    detail = llms_svc.get_llm(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"LLM connection '{name}' not found.")
    report = llms_svc.test_llm(name)
    return templates.TemplateResponse(
        request,
        "llm_detail.html",
        {
            "llm": detail,
            "test_result": report,
            "routing": settings_svc.get_settings().llm,
        },
    )


@router.post("/llms/{name}/delete", response_class=HTMLResponse)
def llm_delete_post(
    request: Request, name: str, _csrf: None = Depends(require_csrf)
) -> RedirectResponse:
    """Delete an LLM connection."""
    try:
        llms_svc.delete_llm(name, actor="web")
    except llms_svc.LlmError as exc:
        return _err(f"/llms/{quote(name)}", exc)
    return RedirectResponse(url="/llms?flash=Deleted", status_code=303)


@router.get("/llms/{name}", response_class=HTMLResponse)
def llm_detail_page(
    request: Request, name: str, error: str | None = None, flash: str | None = None
) -> HTMLResponse:
    """Render one LLM connection (masked key)."""
    detail = llms_svc.get_llm(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"LLM connection '{name}' not found.")
    return templates.TemplateResponse(
        request,
        "llm_detail.html",
        {
            "llm": detail,
            "test_result": None,
            "error": error,
            "flash": flash,
            "routing": settings_svc.get_settings().llm,
        },
    )


@router.post("/llms/{name}", response_class=HTMLResponse)
async def llm_update_post(
    request: Request, name: str, _csrf: None = Depends(require_csrf)
) -> RedirectResponse:
    """Update an LLM connection from the edit form."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    provider = form.get("provider", "openai_compatible")
    role = form.get("role", "general")
    args, secrets = _split_form(form)
    try:
        llms_svc.write_llm(
            name,
            LlmWrite(provider=provider, role=role, args=args, secrets=secrets, secret_ref=None),
            actor="web",
        )
    except (llms_svc.LlmError, ValueError) as exc:
        return _err(f"/llms/{quote(name)}/edit", exc)
    return RedirectResponse(url=f"/llms/{quote(name)}?flash=Saved", status_code=303)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web app.")
