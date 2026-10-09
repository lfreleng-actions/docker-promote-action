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
from scripts.latest import POLICIES
from scripts.refs import (
    DIGEST,
    MAX_PATH,
    PATH,
    TAG,
    base_problem,
    canonical_repository,
    login_endpoint,
)

# 'latest' applies the latest rule alone, to images already pushed.
MODES = ("promote", "verify", "latest")
ON_CONFLICT = ("fail", "overwrite")
# The lanes' constraint: registry accounts use '@' and '+' (Artifactory
# SaaS identities are often email addresses).
_USERNAME = re.compile(r"[A-Za-z0-9._@+-]+")

CONTAINERS_ERROR = (
    "containers_json must be a non-empty JSON array of {name, version} objects "
    "with string values, as docker-workflows' check-release job emits"
)
IMAGES_ERROR = (
    "images_json must be a non-empty JSON array of {image, digest} objects "
    "with string values, as docker-workflows' build job's pushed output"
)
# Inputs naming what promote and verify copy, unused by mode: latest.
PROMOTION_INPUTS = ("containers_json", "pull_registry", "push_registry", "namespace")


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


@dataclass(frozen=True)
class Pushed:
    """An image already released under release_tag, for mode: latest."""

    image: str
    digest: str


def _image_problem(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return "is not an object"
    for key in ("image", "digest"):
        if not isinstance(entry.get(key), str):
            return f"needs a string '{key}'"
    image, digest = entry["image"], entry["digest"]
    if (
        "/" not in image
        or base_problem(image)
        or len(image.split("/", 1)[1]) > MAX_PATH
    ):
        return (
            f"has an 'image' that is not an image repository (a registry host and "
            f"repository path, with no tag or digest): '{image}'"
        )
    if not DIGEST.fullmatch(digest):
        return f"has a 'digest' that is not a digest: '{digest}'"
    return None


def parse_images(raw: str) -> list[Pushed]:
    """The images of images_json, checked in full; other keys are ignored."""
    try:
        data = json.loads(raw)
    except ValueError as err:
        raise ActionError(IMAGES_ERROR) from err
    if not isinstance(data, list) or not data:
        raise ActionError(IMAGES_ERROR)
    images: list[Pushed] = []
    for index, entry in enumerate(data):
        problem = _image_problem(entry)
        if problem:
            raise ActionError(f"{IMAGES_ERROR}; entry #{index + 1} {problem}")
        images.append(Pushed(entry["image"], entry["digest"]))
    repositories = [canonical_repository(image.image) for image in images]
    duplicates = sorted({r for r in repositories if repositories.count(r) > 1})
    if duplicates:
        # Each repository has one 'latest'.
        raise ActionError(
            f"images_json names {', '.join(duplicates)} more than once; each "
            "repository has one latest"
        )
    return images


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


def _namespace(raw: str) -> str:
    namespace = raw.strip()
    if namespace and not PATH.fullmatch(namespace):
        raise ActionError(
            f"namespace '{namespace}' is not a repository path (lowercase "
            "alphanumeric runs joined by '.', '_', '__' or '-', in '/'-separated "
            "parts, with no leading, trailing or doubled '/')"
        )
    return namespace


def _choice(name: str, raw: str, choices: tuple[str, ...]) -> str:
    value = raw.strip() or choices[0]
    if value not in choices:
        raise ActionError(f"{name} must be one of {', '.join(choices)}; got '{value}'")
    return value


def _containers(
    pull: str, push: str, namespace: str, release_tag: str
) -> list[Container]:
    """containers_json, resolved against the registry bases."""
    containers = []
    for name, version in parse_containers(gha.env("INPUT_CONTAINERS_JSON")):
        # Release-file names are relative to the namespace, as in
        # global-jjb's release-job.sh.
        relative = f"{namespace}/{name}" if namespace else name
        image = f"{push}/{relative}"
        for repository in (f"{pull}/{relative}", image):
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
                source=f"{pull}/{relative}:{version}",
                destination=f"{image}:{release_tag}",
                image=image,
            )
        )
    return containers


@dataclass(frozen=True)
class Settings:
    """The action inputs, parsed and validated."""

    containers: tuple[Container, ...]
    release_tag: str
    pull_registry: str
    push_registry: str
    namespace: str = ""
    push_latest: bool = False
    latest_policy: str = "highest"
    dry_run: bool = False
    mode: str = "promote"
    on_conflict: str = "fail"
    registry_user: str = ""
    registry_password: str = ""
    install_crane: bool = True
    summary: bool = True
    # mode: latest's subjects, in place of containers.
    images: tuple[Pushed, ...] = ()

    @property
    def endpoints(self) -> tuple[str, ...]:
        """Distinct login endpoints: pull registry first, or each image's host."""
        if self.mode == "latest":
            return tuple(dict.fromkeys(login_endpoint(i.image) for i in self.images))
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
        return not self.dry_run and self.mode != "verify"

    @property
    def moves_latest(self) -> bool:
        """Whether the run decides on 'latest': push_latest, or mode: latest."""
        return self.push_latest or self.mode == "latest"

    def double_prefixed(self) -> list[Container]:
        """Containers whose release-file name already starts with the namespace."""
        if not self.namespace:
            return []
        prefix = f"{self.namespace}/"
        return [c for c in self.containers if c.name.startswith(prefix)]

    @classmethod
    def from_env(cls) -> Settings:
        """Read and cross-check the INPUT_* variables."""
        env = gha.env
        mode = _choice("mode", env("INPUT_MODE"), MODES)
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
        images: tuple[Pushed, ...] = ()
        if mode == "latest":
            for name in PROMOTION_INPUTS:
                if env(f"INPUT_{name.upper()}").strip():
                    raise ActionError(
                        f"{name} does not apply to mode: latest, which moves "
                        "latest for the images already pushed in images_json"
                    )
            images = tuple(parse_images(env("INPUT_IMAGES_JSON")))
            pull = push = namespace = ""
            containers: list[Container] = []
        else:
            if env("INPUT_IMAGES_JSON").strip():
                raise ActionError(
                    "images_json applies only to mode: latest; promote and verify "
                    "take containers_json"
                )
            pull = _base("pull_registry", env("INPUT_PULL_REGISTRY"))
            push = _base("push_registry", env("INPUT_PUSH_REGISTRY"))
            namespace = _namespace(env("INPUT_NAMESPACE"))
            containers = _containers(pull, push, namespace, release_tag)
        settings = cls(
            containers=tuple(containers),
            release_tag=release_tag,
            pull_registry=pull,
            push_registry=push,
            namespace=namespace,
            push_latest=gha.env_bool("INPUT_PUSH_LATEST", "push_latest", False),
            latest_policy=_choice(
                "latest_policy", env("INPUT_LATEST_POLICY"), POLICIES
            ),
            dry_run=gha.env_bool("INPUT_DRY_RUN", "dry_run", False),
            mode=mode,
            on_conflict=_choice("on_conflict", env("INPUT_ON_CONFLICT"), ON_CONFLICT),
            registry_user=env("INPUT_REGISTRY_USER").strip(),
            registry_password=env("INPUT_REGISTRY_PASSWORD"),
            install_crane=gha.env_bool("INPUT_INSTALL_CRANE", "install_crane", True),
            summary=gha.env_bool("INPUT_SUMMARY", "summary", True),
            images=images,
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
