# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The crane CLI, as promotion calls it.

crane exits 1 for every failure, so whether a tag is absent comes from
the registry's own error code in crane's message. Only a registry
saying the manifest or repository is unknown counts as absent; an
authentication, network or server failure is an error. Read as
absent, an unauthorised destination check would let a copy go ahead
over a tag that already exists.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scripts import gha
from scripts.gha import ActionError

# The OCI distribution-spec codes for "no such manifest/repository",
# and the bare 404 crane reports when a registry sends no error body:
# crane appends any unstructured body after the colon, and a 404 with
# one (a denial, say) is not proof of absence.
_ABSENT = re.compile(
    r"MANIFEST_UNKNOWN|NAME_UNKNOWN"
    r"|unexpected status code 404 Not Found(:| \(HEAD responses have no body.*\))?$"
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}|sha384:[0-9a-f]{96}|sha512:[0-9a-f]{128}")


@dataclass(frozen=True)
class Outcome:
    """A finished crane call: status and its decisive message."""

    status: int
    detail: str

    @property
    def ok(self) -> bool:
        """Whether crane succeeded."""
        return self.status == 0


def _detail(output: str) -> str:
    """The line explaining a failure: crane's last 'Error:' line."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("Error:")]
    return (errors or lines or ["no error output"])[-1]


class Crane:
    """Runs one crane binary, optionally against a private docker config."""

    def __init__(self, binary: str, docker_config: str = "") -> None:
        self.binary = binary
        self.docker_config = docker_config

    def _env(self) -> Mapping[str, str]:
        # crane reads none of the action's inputs; dropping them keeps
        # registry_password out of every child process's environment.
        env = {k: v for k, v in os.environ.items() if not k.startswith("INPUT_")}
        if self.docker_config:
            env["DOCKER_CONFIG"] = self.docker_config
        return env

    def _capture(self, args: Sequence[str], stdin: str = "") -> Outcome:
        proc = subprocess.run(
            [self.binary, *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=self._env(),
            check=False,
        )
        if proc.returncode != 0:
            return Outcome(proc.returncode, _detail(proc.stderr + proc.stdout))
        return Outcome(0, proc.stdout.strip())

    def _stream(self, args: Sequence[str]) -> Outcome:
        """Run crane with its progress in the log, line by line.

        Error text can come from the registry, so the runner's command
        processing stops for the duration: a line of it starting '::'
        is then printed, not executed.
        """
        lines: list[str] = []
        token = secrets.token_hex(16)
        print(f"::stop-commands::{token}", flush=True)
        try:
            with subprocess.Popen(
                [self.binary, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                env=self._env(),
            ) as proc:
                for line in proc.stdout or ():
                    line = line.rstrip("\n")
                    lines.append(line)
                    gha.log(line)
        finally:
            print(f"::{token}::", flush=True)
        return Outcome(proc.returncode, _detail("\n".join(lines)))

    def digest(self, ref: str) -> str | None:
        """The manifest digest the registry holds for ``ref``; None if absent."""
        outcome = self._capture(["digest", ref])
        if not outcome.ok:
            if _ABSENT.search(outcome.detail):
                return None
            raise ActionError(f"cannot read {ref}: {outcome.detail}")
        if not _DIGEST.fullmatch(outcome.detail):
            raise ActionError(
                f"cannot read {ref}: crane digest printed '{outcome.detail}', "
                "not a digest"
            )
        return outcome.detail

    def copy(self, source: str, destination: str, no_clobber: bool) -> Outcome:
        """Copy every manifest under ``source`` to ``destination``, as is.

        With ``no_clobber`` crane refuses a destination tag that exists,
        even one already holding the same digest.
        """
        return self._stream(
            ["copy", *(["--no-clobber"] if no_clobber else []), source, destination]
        )

    def tag(self, ref: str, tag: str) -> Outcome:
        """Point ``tag`` in ref's repository at ref's manifest."""
        return self._stream(["tag", ref, tag])

    def login(self, endpoint: str, user: str, password: str) -> None:
        """Store a credential for ``endpoint``; the password goes on stdin.

        crane does not check the credential here: a wrong one surfaces
        as UNAUTHORIZED on the first read.
        """
        outcome = self._capture(
            ["auth", "login", endpoint, "--username", user, "--password-stdin"],
            stdin=password,
        )
        if not outcome.ok:
            raise ActionError(f"crane auth login {endpoint} failed: {outcome.detail}")
