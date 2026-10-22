"""Role-based access control definitions for Ovid2 web interface.

Defines Role and Capability enums, the ROLE_CAPABILITIES mapping,
and utility functions for checking permissions.
"""

from enum import Enum


class Role(str, Enum):
    """User roles in the system."""

    SYSTEM_ADMIN = "system_admin"
    PIPELINE_OPERATOR = "pipeline_operator"
    VIEWER = "viewer"
    GUEST = "guest"
    PENDING = "pending"


class Capability(str, Enum):
    """Permissions that can be granted to roles."""

    VIEW_PUBLIC = "view_public"
    VIEW_RUNS = "view_runs"
    VIEW_PIPELINE_DATA = "view_pipeline_data"
    RUN_REPORTS = "run_reports"
    SUBMIT_JOBS = "submit_jobs"
    MANAGE_CONFIG = "manage_config"
    MANAGE_SECRETS = "manage_secrets"
    MANAGE_USERS = "manage_users"
    ADMIN_ALL = "admin_all"
    USE_LLM = "use_llm"


# Build role capabilities by starting from lowest and adding permissions
_guest_caps = frozenset([Capability.VIEW_PUBLIC])
_viewer_caps = frozenset(
    list(_guest_caps) + [Capability.VIEW_RUNS, Capability.RUN_REPORTS, Capability.USE_LLM]
)
_pipeline_operator_caps = frozenset(
    list(_viewer_caps) + [Capability.VIEW_PIPELINE_DATA, Capability.SUBMIT_JOBS]
)
_system_admin_caps = frozenset(list(Capability))  # All capabilities

ROLE_CAPABILITIES = {
    Role.PENDING: frozenset(),
    Role.GUEST: _guest_caps,
    Role.VIEWER: _viewer_caps,
    Role.PIPELINE_OPERATOR: _pipeline_operator_caps,
    Role.SYSTEM_ADMIN: _system_admin_caps,
}


def has_capability(role: Role, cap: Capability) -> bool:
    """Check if a role has a specific capability.

    Args:
        role: The role to check
        cap: The capability to check for

    Returns:
        True if the role has the capability, False otherwise
    """
    return cap in ROLE_CAPABILITIES[role]
