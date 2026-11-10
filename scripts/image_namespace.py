# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""mode: namespace's rule: resolve and check the image namespace.

The docker-workflows lanes applied this rule in a bash step, "Resolve
image namespace"; the behaviour and error wording follow it.
"""

from __future__ import annotations

from scripts import gha
from scripts.gha import ActionError
from scripts.refs import PATH

MODES = ("none", "auto", "manual")
# Inputs naming images or registries, which mode: namespace refuses.
REFUSED = ("containers_json", "images_json", "pull_registry", "push_registry")
# ASCII A-Z to a-z alone: str.lower() also maps some non-ASCII letters
# to ASCII ones (the Kelvin sign U+212A to 'k').
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def check(namespace: str, source: str = "") -> str:
    """``namespace``, refused unless empty or a repository path."""
    if namespace and not PATH.fullmatch(namespace):
        raise ActionError(
            f"namespace '{namespace}'{source} is not a repository path (lowercase "
            "alphanumeric runs joined by '.', '_', '__' or '-', in '/'-separated "
            "parts, with no leading, trailing or doubled '/')"
        )
    return namespace


def read() -> tuple[str, str]:
    """namespace_mode and the namespace it resolves to, checked."""
    # Exactly as given: unset reads as 'none', an explicit '' is refused.
    mode = gha.env("INPUT_NAMESPACE_MODE", "none")
    if mode not in MODES:
        raise ActionError(
            f"Invalid namespace_mode '{mode}' (expected none, auto or manual)"
        )
    dockerhub_publish = gha.env_bool(
        "INPUT_DOCKERHUB_PUBLISH", "dockerhub_publish", False
    )
    return mode, _resolve(mode, dockerhub_publish)


def _resolve(mode: str, dockerhub_publish: bool) -> str:
    """mode: namespace's rule, as the docker-workflows lanes apply it."""
    # namespace and repository as given, untrimmed, as the lanes read
    # them: whitespace is set (check 1) or outside the grammar (check 3).
    given = gha.env("INPUT_NAMESPACE")
    if given and mode != "manual":
        raise ActionError(
            f"namespace '{given}' is set but namespace_mode is '{mode}'; set "
            "namespace_mode to 'manual' or clear namespace"
        )
    if mode == "manual" and not given:
        raise ActionError("namespace_mode 'manual' requires the namespace input")
    namespace = given
    if mode == "auto":
        repository = gha.env("INPUT_REPOSITORY")
        owner, slash, name = repository.partition("/")
        if not owner or not slash or not name:
            raise ActionError(
                "namespace_mode 'auto' requires the repository input as owner/name; "
                f"got '{repository}'"
            )
        namespace = owner.translate(_ASCII_LOWER)
    check(namespace, f" from namespace_mode {mode}")
    if dockerhub_publish and (not namespace or "/" in namespace):
        raise ActionError(
            "dockerhub_publish requires namespace_mode 'auto' or 'manual' resolving "
            "to a single path component (the Docker Hub organisation or user); got "
            f"'{namespace}' from namespace_mode {mode}"
        )
    return namespace


def refuse_inputs() -> None:
    """Refuse mode: namespace's own inputs, set away from their defaults."""
    if gha.env("INPUT_NAMESPACE_MODE").strip() not in ("", "none"):
        raise ActionError("namespace_mode applies only to mode: namespace")
    if gha.env("INPUT_REPOSITORY").strip():
        raise ActionError("repository applies only to mode: namespace")
    if gha.env_bool("INPUT_DOCKERHUB_PUBLISH", "dockerhub_publish", False):
        raise ActionError("dockerhub_publish applies only to mode: namespace")
