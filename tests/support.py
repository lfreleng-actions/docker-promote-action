# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Shared helpers: a sandbox with the fake crane, and both runners."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEGACY = ROOT / "tests" / "legacy"

# crane calls that change registry state.
MUTATING = ("copy", "cp", "tag")


def digest(label: str) -> str:
    """A stable, well-formed sha256 digest for a test label."""
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


@dataclass
class Run:
    """The observable result of one invocation."""

    status: int
    outputs: dict[str, str]
    annotations: list[str]
    stdout: str
    summary: str
    calls: list[list[str]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def mutations(self) -> list[list[str]]:
        """State-changing crane calls, in order."""
        return [call for call in self.calls if call[:1] and call[0] in MUTATING]

    @property
    def tags(self) -> dict[str, str]:
        """Every registry tag and the digest it points at, after the run."""
        return dict(self.state.get("tags", {}))

    def json(self, key: str) -> Any:
        """A JSON output, parsed."""
        return json.loads(self.outputs[key])


def parse_outputs(text: str) -> dict[str, str]:
    """Parse GITHUB_OUTPUT in both the ``k=v`` and heredoc forms."""
    outputs: dict[str, str] = {}
    lines = iter(line.removesuffix("\r") for line in text.split("\n"))
    for line in lines:
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delimiter = line.split("<<", 1)
            body: list[str] = []
            for item in lines:
                if item == delimiter:
                    break
                body.append(item)
            outputs[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
    return outputs


class Sandbox:
    """A scratch home, a fake crane on PATH and its registry state."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.temp = root / "runner-temp"
        self.temp.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        crane = self.bin / "crane"
        crane.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests" / "fakecrane.py"}" "$@"\n'
        )
        crane.chmod(0o755)
        self.state_file = root / "state.json"
        self.state_file.write_text("{}")

    def state(self) -> dict[str, Any]:
        """The fake registry state."""
        return dict(json.loads(self.state_file.read_text()))

    def seed(self, **values: Any) -> None:
        """Merge values into the fake registry state."""
        current = self.state()
        for key, value in values.items():
            if isinstance(value, dict):
                current.setdefault(key, {}).update(value)
            else:
                current[key] = value
        self.state_file.write_text(json.dumps(current))

    def stage(self, ref: str, label: str, children: Sequence[str] = ()) -> str:
        """Put an image (an index when it has children) under ``ref``."""
        manifest = digest(label)
        child_digests = [digest(f"{label}/{child}") for child in children]
        repository = ref.rsplit(":", 1)[0]
        state = self.state()
        state.setdefault("tags", {})[ref] = manifest
        held = state.setdefault("manifests", {}).setdefault(repository, [])
        held.extend(d for d in [manifest, *child_digests] if d not in held)
        if child_digests:
            state.setdefault("children", {})[manifest] = child_digests
        self.state_file.write_text(json.dumps(state))
        return manifest

    def invoke(self, command: list[str], env: Mapping[str, str]) -> Run:
        """Run ``command`` in the workspace against the fake crane."""
        scratch = pathlib.Path(tempfile.mkdtemp(dir=self.root))
        output, summary, log = scratch / "output", scratch / "summary", scratch / "log"
        for path in (output, summary, log):
            path.touch()
        full_env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "LC_ALL": "C",
            "HOME": str(self.root),
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            "FAKECRANE_STATE": str(self.state_file),
            "FAKECRANE_LOG": str(log),
            **env,
        }
        proc = subprocess.run(
            command,
            cwd=self.workspace,
            env=full_env,
            capture_output=True,
            text=True,
            check=False,
        )
        return Run(
            status=proc.returncode,
            outputs=parse_outputs(output.read_text(encoding="utf-8")),
            annotations=[
                line for line in proc.stdout.splitlines() if line.startswith("::")
            ],
            stdout=proc.stdout + proc.stderr,
            summary=summary.read_text(encoding="utf-8"),
            calls=[json.loads(line) for line in log.read_text().splitlines() if line],
            state=self.state(),
        )


def run_legacy(sandbox: Sandbox, script: str, **env: str) -> Run:
    """Run a vendored lane step body, as the runner does."""
    return sandbox.invoke(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(LEGACY / script)],
        env,
    )


def run_action(
    sandbox: Sandbox, env: Mapping[str, str] | None = None, /, **inputs: str
) -> Run:
    """Run the action's entry point exactly as action.yaml does.

    ``inputs`` become INPUT_* variables; ``env`` passes through as is.
    The fake crane on PATH stands in for the pinned download.
    """
    full = {f"INPUT_{key.upper()}": value for key, value in inputs.items()}
    full.setdefault("INPUT_INSTALL_CRANE", "false")
    full.update(env or {})
    return sandbox.invoke([sys.executable, "-I", str(ROOT / "entrypoint.py")], full)


class SandboxTestCase(unittest.TestCase):
    """A test case with a fresh sandbox per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = Sandbox(pathlib.Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()
