"""Web-tier services layer.

Each module exposes small, side-effecting functions that FastAPI routes
call. Business logic lives here so that both HTML and JSON endpoints
share exactly one implementation. Nothing in this package imports
FastAPI or Jinja2 — it must remain testable without an HTTP client.
"""
