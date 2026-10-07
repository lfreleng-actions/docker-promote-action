# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Registry bases, login endpoints and image reference grammar.

A registry *base* prefixes image references: ``host[:port]`` with an
optional repository path, such as ``nexus3.onap.org:10002`` (Nexus 3
picks the repository by port) or ``acme.jfrog.io/docker-release``
(Artifactory's repository-path method). A *login endpoint* is the
``host[:port]`` alone: registries authenticate per host, so a path
never takes part in a login. Image references take the base; logins
take the endpoint.
"""

from __future__ import annotations

import ipaddress
import re

_IPV6_HOST = r"\[[0-9A-Fa-f:]+\]"
_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
# Docker's domain grammar: dot-separated labels, each starting and
# ending alphanumeric with hyphens only inside, and an optional port.
REGISTRY = re.compile(rf"({_LABEL}([.]{_LABEL})*|{_IPV6_HOST})(:[0-9]+)?")
# One repository path component, and a '/'-separated path of them.
COMPONENT = re.compile(r"[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*")
PATH = re.compile(rf"{COMPONENT.pattern}(/{COMPONENT.pattern})*")
# Docker's grammar for a tag.
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
# Docker caps the repository path, the part after the registry.
MAX_PATH = 255


def _valid_ipv6(host: str) -> bool:
    """True unless ``host`` brackets something other than an IPv6 address."""
    if not host.startswith("["):
        return True
    literal = host[1:].partition("]")[0]
    try:
        ipaddress.IPv6Address(literal)
    except ValueError:
        return False
    return True


def _valid_port(host: str) -> bool:
    """True unless ``host`` carries a port outside 1-65535."""
    if host.endswith("]") or ":" not in host:
        return True
    return 1 <= int(host.rpartition(":")[2]) <= 65535


def is_registry(component: str) -> bool:
    """Docker's rule: a leading component with '.' or ':', or localhost.

    Anything else is the first path component of a Docker Hub
    repository, so a base without one would silently resolve to
    docker.io: ``myregistry/team`` names ``docker.io/myregistry/team``.
    """
    return "." in component or ":" in component or component == "localhost"


def base_problem(base: str) -> str | None:
    """Why ``base`` is not a usable registry base, or None when it is."""
    if base.endswith("/"):
        return "ends with '/'"
    host, _, path = base.partition("/")
    if not is_registry(host):
        return (
            f"starts with '{host}', which is not a registry host; registry tools "
            "would read it as a Docker Hub namespace (use host[:port], such as "
            "docker.io/onap or localhost:5000)"
        )
    if not REGISTRY.fullmatch(host) or not _valid_ipv6(host) or not _valid_port(host):
        return f"has an invalid registry host '{host}'"
    if path and not PATH.fullmatch(path):
        return (
            f"has an invalid repository path '{path}' (lowercase alphanumeric runs "
            "joined by '.', '_', '__' or '-', in '/'-separated parts)"
        )
    return None


def login_endpoint(base: str) -> str:
    """The ``host[:port]`` a registry base authenticates against.

    The lanes compute this as ``${BASE%%/*}``: everything before the
    first '/'. A Nexus 3 port stays, since it selects the repository;
    an Artifactory repository path goes, since it belongs to the image
    reference rather than the login.
    """
    return base.split("/", 1)[0]


def repository_path(repository: str) -> str:
    """The part of ``repository`` after its registry host."""
    return repository.split("/", 1)[1] if "/" in repository else repository


# The Docker Hub hosts Docker and crane resolve to one registry.
_DOCKER_HUB = ("docker.io", "index.docker.io")


def canonical_repository(repository: str) -> str:
    """``repository`` as the registry resolves it, for comparisons.

    Hosts are case-insensitive, and Docker Hub's familiar forms name
    one repository: docker.io/app, index.docker.io/app and
    docker.io/library/app.
    """
    host, _, path = repository.partition("/")
    host = host.lower()
    if host in _DOCKER_HUB:
        host = _DOCKER_HUB[0]
        if "/" not in path:
            path = f"library/{path}"
    return f"{host}/{path}"
