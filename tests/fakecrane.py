# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A scripted stand-in for the crane CLI, for offline tests.

Installed on PATH as ``crane``, it models the registries promotion
talks to: tags pointing at manifest digests, the manifests each
repository holds (an index brings its children), and hosts that need
a login. Its messages copy real crane 0.22.1's, as measured against a
registry:2 container:

* a missing tag or repository: exit 1, ``MANIFEST_UNKNOWN``
* a host needing credentials: exit 1, ``UNAUTHORIZED``
* ``copy --no-clobber`` over any existing tag, even one holding the
  same digest: exit 1, ``refusing to clobber existing tag``
* ``auth login`` stores whatever credential it is given, unchecked
* ``ls`` of a repository that does not exist: exit 1, ``NAME_UNKNOWN``

State lives in the JSON file ``$FAKECRANE_STATE``; every invocation
appends its argv to ``$FAKECRANE_LOG``, one JSON array per line.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

State = dict[str, Any]


class Failure(Exception):
    """crane's 'Error:' line, exit status 1."""


def _load() -> State:
    path = Path(os.environ["FAKECRANE_STATE"])
    state: State = json.loads(path.read_text()) if path.exists() else {}
    for key in (
        "tags",
        "manifests",
        "children",
        "protected",
        "fail",
        "fail_copy",
        "fail_tag",
        "fail_ls",
        "race",
        "after_copy",
        "after_ls",
    ):
        state.setdefault(key, {})
    return state


def split(ref: str) -> tuple[str, str, str]:
    """(repository, separator, tag or digest) of a reference."""
    if "@" in ref:
        repository, digest = ref.split("@", 1)
        return repository, "@", digest
    if ref.rfind(":") > ref.rfind("/"):
        repository, tag = ref.rsplit(":", 1)
        return repository, ":", tag
    return ref, ":", "latest"


def _url(ref: str) -> str:
    repository, _, version = split(ref)
    host, _, path = repository.partition("/")
    scheme = "http" if host.startswith("localhost") else "https"
    return f"{scheme}://{host}/v2/{path}/manifests/{version}"


def _config() -> dict[str, Any]:
    directory = os.environ.get("DOCKER_CONFIG") or os.path.join(
        os.environ["HOME"], ".docker"
    )
    path = Path(directory, "config.json")
    return json.loads(path.read_text()) if path.exists() else {}


def _authorise(state: State, ref: str) -> None:
    host = ref.split("/", 1)[0]
    wanted = state["protected"].get(host)
    if wanted is None:
        return
    stored = _config().get("auths", {}).get(host, {}).get("auth", "")
    if stored != base64.b64encode(wanted.encode()).decode():
        repository = split(ref)[0].split("/", 1)[1]
        raise Failure(
            f"GET {_url(ref)}: UNAUTHORIZED: authentication required; "
            f"[map[Action:pull Class: Name:{repository} Type:repository]]"
        )


def resolve(state: State, ref: str) -> str:
    """The digest ``ref`` names, as the registry would serve it."""
    for pattern, message in state["fail"].items():
        if pattern in ref:
            raise Failure(f"GET {_url(ref)}: {message}")
    _authorise(state, ref)
    repository, separator, version = split(ref)
    if separator == "@":
        if version in state["manifests"].get(repository, []):
            return version
        detail = f"map[Name:{repository.split('/', 1)[1]} Revision:{version}]"
    else:
        digest = state["tags"].get(ref)
        if digest:
            return str(digest)
        detail = f"map[Tag:{version}]"
    raise Failure(f"GET {_url(ref)}: MANIFEST_UNKNOWN: manifest unknown; {detail}")


def _store(state: State, repository: str, digest: str) -> None:
    held = state["manifests"].setdefault(repository, [])
    for item in [digest, *state["children"].get(digest, [])]:
        if item not in held:
            held.append(item)


def _digest(state: State, args: list[str]) -> int:
    ref = args[-1]
    try:
        digest = resolve(state, ref)
    except Failure:
        print(
            f"HEAD request failed, falling back on GET: HEAD {_url(ref)}: unexpected "
            "status code 404 Not Found (HEAD responses have no body, use GET for "
            "details)",
            file=sys.stderr,
        )
        raise
    print(digest)
    return 0


def _copy(state: State, args: list[str]) -> int:
    no_clobber = "-n" in args or "--no-clobber" in args
    source, destination = [arg for arg in args if not arg.startswith("-")]
    try:
        digest = resolve(state, source)
    except Failure as err:
        raise Failure(f'fetching "{source}": {err}') from err
    _authorise(state, destination)
    if destination in state["race"]:
        # Another writer tags the destination while this copy runs.
        state["tags"][destination] = state["race"].pop(destination)
        _store(state, split(destination)[0], state["tags"][destination])
    if no_clobber:
        print(f"Checking existing tag {destination}", file=sys.stderr)
        existing = state["tags"].get(destination)
        if existing:
            raise Failure(f"refusing to clobber existing tag {destination}@{existing}")
    if destination in state["fail_copy"]:
        raise Failure(state["fail_copy"][destination])
    _store(state, split(destination)[0], digest)
    state["tags"][destination] = state["after_copy"].get(destination, digest)
    print(f"{destination}: digest: {digest} size: 1234", file=sys.stderr)
    return 0


def _tag(state: State, args: list[str]) -> int:
    ref, tag = args
    try:
        digest = resolve(state, ref)
    except Failure as err:
        raise Failure(f'fetching "{ref}": {err}') from err
    target = f"{split(ref)[0]}:{tag}"
    if target in state["fail_tag"]:
        raise Failure(state["fail_tag"][target])
    state["tags"][target] = digest
    print(f"{target}: digest: {digest} size: 1234", file=sys.stderr)
    return 0


def _ls(state: State, args: list[str]) -> int:
    (repository,) = [arg for arg in args if not arg.startswith("-")]
    host, _, path = repository.partition("/")
    scheme = "http" if host.startswith("localhost") else "https"
    url = f"{scheme}://{host}/v2/{path}/tags/list?n=1000"
    failures = {**state["fail"], **state["fail_ls"]}
    for pattern, message in failures.items():
        if pattern in repository:
            raise Failure(f"reading tags for {repository}: GET {url}: {message}")
    _authorise(state, repository)
    tags = sorted(
        tag
        for ref in state["tags"]
        for held, separator, tag in [split(ref)]
        if held == repository and separator == ":"
    )
    if not tags and repository not in state["manifests"]:
        raise Failure(
            f"reading tags for {repository}: GET {url}: NAME_UNKNOWN: repository "
            f"name not known to registry; map[name:{path}]"
        )
    for tag in tags:
        print(tag)
    if repository in state["after_ls"]:
        # Another run releases into the repository once this listing is read.
        for ref, digest in state["after_ls"].pop(repository).items():
            state["tags"][ref] = digest
            _store(state, repository, digest)
    return 0


def _login(args: list[str]) -> int:
    host = args[0]
    user = ""
    for flag in ("-u", "--username"):
        if flag in args:
            user = args[args.index(flag) + 1]
    if "--password-stdin" not in args:
        raise Failure("fakecrane: the password must come on stdin")
    password = sys.stdin.read()
    directory = Path(
        os.environ.get("DOCKER_CONFIG") or os.path.join(os.environ["HOME"], ".docker")
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    config.setdefault("auths", {})[host] = {"auth": token}
    path.write_text(json.dumps(config))
    print(f"logged in via {path}", file=sys.stderr)
    return 0


def _dispatch(state: State, argv: list[str]) -> int:
    command, args = argv[0], argv[1:]
    if command == "digest":
        return _digest(state, args)
    if command in ("copy", "cp"):
        return _copy(state, args)
    if command == "tag":
        return _tag(state, args)
    if command == "ls":
        return _ls(state, args)
    if argv[:2] == ["auth", "login"]:
        return _login(argv[2:])
    print(f"fakecrane: unsupported command {argv!r}", file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    with open(os.environ["FAKECRANE_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")
    state = _load()
    try:
        return _dispatch(state, argv)
    except Failure as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    finally:
        Path(os.environ["FAKECRANE_STATE"]).write_text(json.dumps(state, indent=1))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
