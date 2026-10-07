# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""A pinned crane, downloaded and checked against embedded digests.

imjasonh/setup-crane, which the lane used, installs whichever release
is latest, unverified, and logs in to ghcr.io with the job token,
replacing any ghcr.io login the caller made. Here the version is fixed
and each archive must match the SHA-256 recorded below, taken from the
release's checksums.txt; anything else is refused before it runs.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import tarfile
import time
import urllib.request

from scripts.gha import ActionError

VERSION = "v0.22.1"
URL = (
    "https://github.com/google/go-containerregistry/releases/download/"
    "{version}/go-containerregistry_{system}_{machine}.tar.gz"
)
SHA256 = {
    (
        "Linux",
        "x86_64",
    ): "0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0",
    (
        "Linux",
        "arm64",
    ): "898c0cff975f898a33e8c4580bdafb0e7c02c7faa33374e946762f97c4ab7110",
    (
        "Darwin",
        "x86_64",
    ): "6fedd06a648c11335f0e8b9547e4783002c30e9336047fec292942cd518bb799",
    (
        "Darwin",
        "arm64",
    ): "2231fc8df8806d20d680ff1225db44e095a55dd6ac1ae8eced4faf4b278b78fb",
}
_MACHINES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
_ATTEMPTS = 3


def target() -> tuple[str, str]:
    """The (system, machine) pair naming this runner's release archive."""
    system = platform.system()
    machine = _MACHINES.get(platform.machine().lower(), platform.machine())
    if (system, machine) not in SHA256:
        raise ActionError(
            f"no pinned crane for {system}/{machine}; install crane on PATH and "
            "set install_crane: false"
        )
    return system, machine


def _download(url: str) -> bytes:
    attempt = 1
    while True:
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return bytes(response.read())
        except OSError as err:
            if attempt >= _ATTEMPTS:
                raise ActionError(f"cannot download crane from {url}: {err}") from err
            time.sleep(2 * attempt)
            attempt += 1


def install(directory: str) -> str:
    """Install crane into ``directory``; returns the binary's path."""
    system, machine = target()
    url = URL.format(version=VERSION, system=system, machine=machine)
    archive = _download(url)
    actual = hashlib.sha256(archive).hexdigest()
    expected = SHA256[(system, machine)]
    if actual != expected:
        raise ActionError(
            f"crane archive {url} has SHA-256 {actual}, not the pinned {expected}; "
            "refusing to run it"
        )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.extractfile("crane")
        if member is None:
            raise ActionError(f"crane archive {url} holds no crane binary")
        binary = member.read()
    path = os.path.join(directory, "crane")
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700), "wb") as out:
        out.write(binary)
    return path
