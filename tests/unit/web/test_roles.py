"""Unit tests for role-based access control definitions."""

from ovid2.web.auth.roles import ROLE_CAPABILITIES, Capability, Role, has_capability


def test_role_capabilities_containment():
    """Test that role capabilities form a proper hierarchy."""
    # PENDING has no capabilities
    assert ROLE_CAPABILITIES[Role.PENDING] == frozenset()

    # GUEST has VIEW_PUBLIC
    assert ROLE_CAPABILITIES[Role.GUEST] == frozenset([Capability.VIEW_PUBLIC])

    # VIEWER has GUEST capabilities + VIEW_RUNS + RUN_REPORTS + USE_LLM
    expected_viewer = frozenset(
        [
            Capability.VIEW_PUBLIC,
            Capability.VIEW_RUNS,
            Capability.RUN_REPORTS,
            Capability.USE_LLM,
        ]
    )
    assert ROLE_CAPABILITIES[Role.VIEWER] == expected_viewer

    # PIPELINE_OPERATOR has VIEWER capabilities + VIEW_PIPELINE_DATA + SUBMIT_JOBS
    expected_operator = frozenset(
        [
            Capability.VIEW_PUBLIC,
            Capability.VIEW_RUNS,
            Capability.RUN_REPORTS,
            Capability.USE_LLM,
            Capability.VIEW_PIPELINE_DATA,
            Capability.SUBMIT_JOBS,
        ]
    )
    assert ROLE_CAPABILITIES[Role.PIPELINE_OPERATOR] == expected_operator

    # SYSTEM_ADMIN has all capabilities
    assert ROLE_CAPABILITIES[Role.SYSTEM_ADMIN] == frozenset(list(Capability))


def test_role_capabilities_subset_relationships():
    """Test that capabilities are properly nested."""
    assert ROLE_CAPABILITIES[Role.GUEST].issubset(ROLE_CAPABILITIES[Role.VIEWER])
    assert ROLE_CAPABILITIES[Role.VIEWER].issubset(ROLE_CAPABILITIES[Role.PIPELINE_OPERATOR])
    assert ROLE_CAPABILITIES[Role.PIPELINE_OPERATOR].issubset(ROLE_CAPABILITIES[Role.SYSTEM_ADMIN])


def test_every_capability_appears():
    """Test that every capability appears in at least one role's set."""
    all_role_capabilities = set()
    for caps in ROLE_CAPABILITIES.values():
        all_role_capabilities.update(caps)

    assert all_role_capabilities == set(Capability)


def test_has_capability():
    """Test the has_capability function."""
    # Viewer can run reports
    assert has_capability(Role.VIEWER, Capability.RUN_REPORTS) is True

    # Guest cannot submit jobs
    assert has_capability(Role.GUEST, Capability.SUBMIT_JOBS) is False

    # Pending cannot view public
    assert has_capability(Role.PENDING, Capability.VIEW_PUBLIC) is False

    # System admin has all capabilities
    for cap in Capability:
        assert has_capability(Role.SYSTEM_ADMIN, cap) is True
