"""Datavant token transfer hook (stub for Phase 2).

Full implementation lands in Phase 8 alongside pipeline porting. The stub
exists so :mod:`ovid2.core.pipeline` can import it; calling it always
raises to signal that the tool is not yet wired up.
"""

from __future__ import annotations


def transfer_tokens_to_pcornet(
    pipeline: str,
    batch_id: str,
    input_dir: str,
    file_glob: str,
) -> str:
    """Invoke the Datavant transform-tokens binary. Not yet ported.

    Args:
        pipeline: Pipeline name.
        batch_id: Batch identifier.
        input_dir: Directory containing raw hash-token files.
        file_glob: Glob for the raw files.

    Returns:
        Path to the directory containing the transformed output.

    Raises:
        NotImplementedError: Always — placeholder for Phase 8.
    """
    raise NotImplementedError(
        "Datavant token transfer is not yet ported to Ovid2. Track progress via CHANGELOG.md (P8)."
    )
