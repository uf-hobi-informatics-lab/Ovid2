"""``ovid2 image`` — build and push the Ovid2 Docker image.

Thin wrapper around ``docker build`` / ``docker push`` that reads defaults
from :mod:`ovid2.config.settings`. All heavy lifting is deferred to the
Docker CLI so this module has no runtime dependencies beyond ``subprocess``.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from ovid2.config.settings import DOCKER_IMAGE


def _require_docker() -> str:
    """Return the resolved ``docker`` executable or raise ``FileNotFoundError``."""
    docker = shutil.which("docker")
    if docker is None:
        raise FileNotFoundError(
            "`docker` is not on PATH. Install Docker before running 'ovid2 image'."
        )
    return docker


def build_image(*, tag: str, context: Path, dockerfile: Path | None = None) -> int:
    """Invoke ``docker build`` and return its exit code.

    Args:
        tag: Full image tag (``name:version``).
        context: Build context directory.
        dockerfile: Optional path to a ``Dockerfile`` (defaults to
            ``<context>/Dockerfile``).

    Returns:
        Docker's exit status.
    """
    docker = _require_docker()
    cmd: list[str] = [docker, "build", "-t", tag]
    if dockerfile is not None:
        cmd += ["-f", str(dockerfile)]
    cmd.append(str(context))
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=False).returncode


def push_image(*, tag: str) -> int:
    """Invoke ``docker push`` and return its exit code."""
    docker = _require_docker()
    cmd = [docker, "push", tag]
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=False).returncode


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``image`` subcommand and its actions."""
    parser = subparsers.add_parser(
        "image",
        help="Build or push the Ovid2 Docker image.",
    )
    action = parser.add_subparsers(dest="action", metavar="ACTION", required=True)

    build = action.add_parser("build", help="Build the image locally.")
    build.add_argument("--tag", default=DOCKER_IMAGE, help="Image tag (default: $OVID2_DOCKER_IMAGE).")
    build.add_argument(
        "--context",
        default=".",
        help="Docker build context directory (default: current directory).",
    )
    build.add_argument(
        "--dockerfile",
        default=None,
        help="Path to Dockerfile (default: <context>/docker/Dockerfile).",
    )
    build.set_defaults(_command="image", _entry=_entry_build)

    push = action.add_parser("push", help="Push the image to a registry.")
    push.add_argument("--tag", default=DOCKER_IMAGE, help="Image tag to push.")
    push.set_defaults(_command="image", _entry=_entry_push)


def _entry_build(args: argparse.Namespace) -> int:
    """Handle ``ovid2 image build``."""
    context = Path(args.context).resolve()
    dockerfile = Path(args.dockerfile).resolve() if args.dockerfile else context / "docker" / "Dockerfile"
    if not dockerfile.is_file():
        print(f"[ovid2 image] Dockerfile not found: {dockerfile}")
        return 1
    return int(build_image(tag=args.tag, context=context, dockerfile=dockerfile))


def _entry_push(args: argparse.Namespace) -> int:
    """Handle ``ovid2 image push``."""
    return int(push_image(tag=args.tag))
