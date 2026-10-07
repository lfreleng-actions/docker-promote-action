# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Action inputs: parsing, validation and the promotion plan.

Everything here runs before the first registry call, so a malformed
release file or input fails the step having touched nothing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from scripts import gha
from scripts.gha import ActionError
from scripts.refs import (
    MAX_PATH,
    PATH,
    TAG,
    base_problem,
    canonical_repository,
    login_endpoint,
)

MODES = ("promote", "verify")
ON_CONFLICT = ("fail", "overwrite")
# The lanes' constraint: registry accounts use '@' and '+' (Artifactory
# SaaS identities are often email addresses).
_USERNAME = re.compile(r"[A-Za-z0-9._@+-]+")

CONTAINERS_ERROR = (
    "containers_json must be a non-empty JSON array of {name, version} objects "
    "with string values, as docker-workflows' check-release job emits"
)


@dataclass(frozen=True)
class Container:
    """One release file entry, resolved against the registry bases."""

    name: str
    version: str
    source: str
    destination: str
    # The destination repository: with the digest, the signing subject.
    image: str

    @property
    def source_repository(self) -> str:
        """The source reference without its tag."""
        return self.source.rsplit(":", 1)[0]

    @property
    def same_reference(self) -> bool:
        """Whether source and destination name one tag in one repository.

        The staged version is then the release tag on the release
        registry: nothing to copy, and most likely a release file
        naming the release rather than the staged build.
        """
        source_tag = self.source.rsplit(":", 1)[1]
        release_tag = self.destination.rsplit(":", 1)[1]
        return source_tag == release_tag and canonical_repository(
            self.source_repository
        ) == canonical_repository(self.image)


def _container_problem(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return "is not an object"
    for key in ("name", "version"):
        if not isinstance(entry.get(key), str):
            return f"needs a string '{key}'"
    if not PATH.fullmatch(entry["name"]):
        return (
            "has a 'name' that is not a repository path (lowercase alphanumeric "
            "runs joined by '.', '_', '__' or '-', in '/'-separated parts)"
        )
    if not TAG.fullmatch(entry["version"]):
        return f"has a 'version' that is not a valid Docker tag: '{entry['version']}'"
    return None


def parse_containers(raw: str) -> list[tuple[str, str]]:
    """The (name, version) pairs of containers_json, checked in full."""
    try:
        data = json.loads(raw)
    except ValueError as err:
        raise ActionError(CONTAINERS_ERROR) from err
    if not isinstance(data, list) or not data:
        raise ActionError(CONTAINERS_ERROR)
    pairs: list[tuple[str, str]] = []
    for index, entry in enumerate(data):
        problem = _container_problem(entry)
        if problem:
            name = entry.get("name") if isinstance(entry, dict) else None
            label = name if isinstance(name, str) and name else f"#{index + 1}"
            raise ActionError(f"{CONTAINERS_ERROR}; entry {label} {problem}")
        pairs.append((entry["name"], entry["version"]))
    names = [name for name, _ in pairs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        # Two entries would promote to one destination, and only the
        # last would survive.
        raise ActionError(
            f"containers_json names {', '.join(duplicates)} more than once; "
            "each name promotes to one destination"
        )
    return pairs


def _base(name: str, raw: str) -> str:
    base = raw.strip()
    if not base:
        raise ActionError(f"{name} is required (a registry base, host[:port][/path])")
    problem = base_problem(base)
    if problem:
        raise ActionError(f"{name} '{base}' {problem}")
    return base


def _choice(name: str, raw: str, choices: tuple[str, ...]) -> str:
    value = raw.strip() or choices[0]
    if value not in choices:
        raise ActionError(f"{name} must be one of {', '.join(choices)}; got '{value}'")
    return value


@dataclass(frozen=True)
class Settings:
    """The action inputs, parsed and validated."""

    containers: tuple[Container, ...]
    release_tag: str
    pull_registry: str
    push_registry: str
    push_latest: bool = False
    dry_run: bool = False
    mode: str = "promote"
    on_conflict: str = "fail"
    registry_user: str = ""
    registry_password: str = ""
    install_crane: bool = True
    summary: bool = True

    @property
    def endpoints(self) -> tuple[str, ...]:
        """Distinct login endpoints, pull registry first."""
        pull, push = (
            login_endpoint(self.pull_registry),
            login_endpoint(self.push_registry),
        )
        return (pull,) if pull == push else (pull, push)

    @property
    def reads(self) -> bool:
        """Whether the run queries registries (everything but a dry run)."""
        return not self.dry_run

    @property
    def writes(self) -> bool:
        """Whether the run may change registry state."""
        return not self.dry_run and self.mode == "promote"

    @classmethod
    def from_env(cls) -> Settings:
        """Read and cross-check the INPUT_* variables."""
        env = gha.env
        release_tag = env("INPUT_RELEASE_TAG").strip()
        if not TAG.fullmatch(release_tag):
            raise ActionError(
                f"release_tag must be a valid Docker tag; got '{release_tag}'"
            )
        if release_tag == "latest":
            # Release tags are immutable here; 'latest' moves by design,
            # so every later release would read as a conflict.
            raise ActionError(
                "release_tag 'latest' is a moving tag; promote to the release "
                "version and set push_latest instead"
            )
        pull = _base("pull_registry", env("INPUT_PULL_REGISTRY"))
        push = _base("push_registry", env("INPUT_PUSH_REGISTRY"))
        containers = []
        for name, version in parse_containers(env("INPUT_CONTAINERS_JSON")):
            image = f"{push}/{name}"
            for repository in (f"{pull}/{name}", image):
                path = repository.split("/", 1)[1]
                if len(path) > MAX_PATH:
                    raise ActionError(
                        f"containers_json entry {name} resolves to {repository}, a "
                        f"{len(path)}-character repository path, over Docker's "
                        f"limit of {MAX_PATH}"
                    )
            containers.append(
                Container(
                    name=name,
                    version=version,
                    source=f"{pull}/{name}:{version}",
                    destination=f"{image}:{release_tag}",
                    image=image,
                )
            )
        settings = cls(
            containers=tuple(containers),
            release_tag=release_tag,
            pull_registry=pull,
            push_registry=push,
            push_latest=gha.env_bool("INPUT_PUSH_LATEST", "push_latest", False),
            dry_run=gha.env_bool("INPUT_DRY_RUN", "dry_run", False),
            mode=_choice("mode", env("INPUT_MODE"), MODES),
            on_conflict=_choice("on_conflict", env("INPUT_ON_CONFLICT"), ON_CONFLICT),
            registry_user=env("INPUT_REGISTRY_USER").strip(),
            registry_password=env("INPUT_REGISTRY_PASSWORD"),
            install_crane=gha.env_bool("INPUT_INSTALL_CRANE", "install_crane", True),
            summary=gha.env_bool("INPUT_SUMMARY", "summary", True),
        )
        settings.check()
        return settings

    def check(self) -> None:
        """Refuse combinations that cannot be honoured."""
        if self.dry_run and self.mode == "verify":
            raise ActionError(
                "dry_run and mode: verify are exclusive: a dry run never contacts a "
                "registry, while verify reads every source and destination"
            )
        if self.registry_password and not self.registry_user:
            raise ActionError("registry_password is set but registry_user is empty")
        if self.registry_user and not self.registry_password:
            # Most likely a credential that failed to load; carrying on
            # anonymously would only fail later, less clearly.
            raise ActionError(
                "registry_user is set but registry_password is empty; pass both to "
                "log in, or neither to use the runner's existing registry logins"
            )
        if self.registry_user and not _USERNAME.fullmatch(self.registry_user):
            raise ActionError(
                "registry_user may hold only letters, digits and '.', '_', '@', '+', '-'"
            )
