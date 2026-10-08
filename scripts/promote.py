# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Registry-side release promotion: check, copy, then move latest.

Each container's staged ``<pull_registry>/<name>:<version>`` copies to
``<push_registry>/<name>:<release_tag>``, manifest for manifest, so a
multi-architecture index keeps its digest. Three phases:

1. Check: every source digest and every destination's state is read
   before anything is written. A missing source, an unreadable
   registry or a release tag already holding other bits fails the run
   here, with nothing published.
2. Copy: each source copies by the digest the check resolved, so a
   staged tag that moves meanwhile cannot change what is released.
   The registry's digest for the destination is read back and must
   match.
3. Latest: only once every container is released does 'latest' move,
   by digest, so a failed copy never leaves a partially-latest release.

A destination already holding the source digest is 'skipped': the
release happened before, as global-jjb's container release job finds
when it re-runs. A source that is the destination itself (the staged
version is the release tag on the release registry) is skipped the
same way, with a notice, since the release file most likely names
the release rather than the staged build.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass

from scripts import gha, install
from scripts.crane import Crane
from scripts.gha import ActionError
from scripts.settings import Container, Settings

# Statuses that count as released, and so may carry 'latest'.
RELEASED = ("promoted", "skipped")


@dataclass
class Result:
    """What happened to one container."""

    container: Container
    status: str = "pending"
    # The staged digest; for promoted and skipped entries, the digest
    # the registry reports for the destination too.
    digest: str = ""

    def record(self) -> dict[str, str]:
        """The entry in the promoted output."""
        container = self.container
        return {
            "name": container.name,
            "source": container.source,
            "destination": container.destination,
            "image": container.image,
            "digest": self.digest,
            "status": self.status,
        }


class Promotion:
    """One run over the release file's containers."""

    def __init__(self, settings: Settings, crane: Crane | None) -> None:
        self.settings = settings
        self.crane = crane
        self.results = [Result(container) for container in settings.containers]
        self.latest: list[str] = []

    def _registry(self) -> Crane:
        if self.crane is None:
            raise ActionError("internal error: a dry run has no registry client")
        return self.crane

    def run(self) -> bool:
        """Run every phase the settings call for; True on success."""
        for container in self._same_reference():
            gha.annotate(
                "notice",
                f"{container.source} and {container.destination} are the same "
                "reference (the staged version is the release tag on the release "
                "registry), so there is nothing to copy and it counts as released; "
                "check the release file if a staged build was meant",
            )
        for container in self.settings.double_prefixed():
            gha.annotate(
                "notice",
                f"{container.name} already starts with namespace "
                f"'{self.settings.namespace}', so it resolves to {container.image}, "
                "probably double-prefixed: release-file names are relative to the "
                "namespace",
            )
        if self.settings.dry_run:
            self._plan()
            return True
        ok = self._check()
        if not self.settings.writes:
            if ok and self.settings.push_latest:
                self.latest = [f"{r.container.image}:latest" for r in self.results]
            return ok
        if ok:
            ok = self._copy() and (not self.settings.push_latest or self._move_latest())
        for result in self.results:
            if result.status == "ready":
                result.status = "pending"
        return ok

    def _plan(self) -> None:
        # The lane's dry-run lines, verbatim: no registry is contacted.
        for result in self.results:
            container = result.container
            result.status = "planned"
            gha.log(
                f"Dry run: would copy {container.source} -> {container.destination}"
            )
        if self.settings.push_latest:
            for result in self.results:
                container = result.container
                gha.log(f"Dry run: would tag {container.destination} as latest")
                self.latest.append(f"{container.image}:latest")

    def _check(self) -> bool:
        """Read every source and destination; True when all may proceed."""
        crane = self._registry()
        ok = True
        for result in self.results:
            container = result.container
            # Read both sides even when one fails, so every problem is
            # reported in one run.
            reads: dict[str, str | None] = {}
            unreadable = False
            for ref in (container.source, container.destination):
                try:
                    reads[ref] = crane.digest(ref)
                except ActionError as err:
                    gha.annotate("error", str(err))
                    unreadable = True
            source = reads.get(container.source)
            existing = reads.get(container.destination)
            if container.source in reads and source is None:
                gha.annotate(
                    "error",
                    f"Staged image {container.source} not found on the pull registry",
                )
            if source:
                result.digest = source
            if unreadable or source is None:
                result.status = "failed" if unreadable else "missing"
                ok = False
                continue
            if existing is None:
                result.status = "ready"
                gha.log(f"{container.source} ({source}) -> {container.destination}")
            elif existing == source:
                result.status = "skipped"
                gha.log(
                    f"{container.destination} already holds {source}; released before"
                )
            elif self.settings.on_conflict == "overwrite":
                result.status = "ready"
                gha.annotate(
                    "warning",
                    f"{container.destination} holds {existing}; on_conflict: overwrite "
                    f"replaces it with {container.source} ({source})",
                )
            else:
                result.status = "conflict"
                gha.annotate("error", _conflict(container, existing, source))
                ok = False
        return ok

    def _copy(self) -> bool:
        """Copy each ready container; stops at the first failure."""
        crane = self._registry()
        no_clobber = self.settings.on_conflict == "fail"
        for result in self.results:
            if result.status != "ready":
                continue
            container = result.container
            gha.group(f"Promote {container.source} -> {container.destination}")
            copied = crane.copy(
                f"{container.source_repository}@{result.digest}",
                container.destination,
                no_clobber=no_clobber,
            )
            gha.endgroup()
            try:
                landed = crane.digest(container.destination)
            except ActionError as err:
                result.status = "failed"
                gha.annotate("error", f"Promoting {container.destination}: {err}")
                return False
            if landed == result.digest:
                # A failed --no-clobber copy over the same digest means
                # a concurrent run released it first.
                result.status = "promoted" if copied.ok else "skipped"
                continue
            if not copied.ok and landed is not None and no_clobber:
                result.status = "conflict"
                gha.annotate("error", _conflict(container, landed, result.digest))
            elif not copied.ok:
                result.status = "failed"
                gha.annotate(
                    "error",
                    f"Copying {container.source} to {container.destination} failed: "
                    f"{copied.detail}",
                )
            else:
                result.status = "failed"
                gha.annotate(
                    "error",
                    f"{container.destination} reads back as {landed or 'absent'} after "
                    f"the copy, not the staged {result.digest}",
                )
            return False
        return True

    def _move_latest(self) -> bool:
        """Point every released image's 'latest' at its release digest."""
        crane = self._registry()
        for result in self.results:
            container = result.container
            gha.group(f"Tag {container.image}:latest")
            tagged = crane.tag(f"{container.image}@{result.digest}", "latest")
            gha.endgroup()
            if not tagged.ok:
                gha.annotate(
                    "error", f"Tagging {container.image}:latest failed: {tagged.detail}"
                )
                return False
            try:
                landed = crane.digest(f"{container.image}:latest")
            except ActionError as err:
                gha.annotate("error", f"Tagging {container.image}:latest: {err}")
                return False
            if landed != result.digest:
                gha.annotate(
                    "error",
                    f"{container.image}:latest reads back as {landed or 'absent'}, "
                    f"not {result.digest}",
                )
                return False
            self.latest.append(f"{container.image}:latest")
        return True

    def outputs(self) -> dict[str, str]:
        """The step outputs, written whether or not the run succeeded."""
        statuses = [result.status for result in self.results]
        return {
            "promoted": _compact([result.record() for result in self.results]),
            "promoted_count": str(statuses.count("promoted")),
            "skipped_count": str(statuses.count("skipped")),
            "latest": _compact(self.latest),
            "pull_endpoint": self.settings.endpoints[0],
            "push_endpoint": self.settings.endpoints[-1],
        }

    def _same_reference(self) -> list[Container]:
        return [c for c in self.settings.containers if c.same_reference]

    def _same_reference_note(self) -> list[str]:
        same = self._same_reference()
        if not same:
            return []
        lines = [
            "Source and destination are the same reference, so nothing is "
            "copied; check the release file if a staged build was meant:",
            "",
        ]
        return lines + [f"- `{c.name}`: `{c.destination}`" for c in same]

    def summary(self) -> str:
        """The step summary in Markdown."""
        lines = ["## Release Promotion", ""]
        if self.settings.dry_run:
            # The lane's dry-run summary, verbatim, then any notice.
            tags = [r.container.destination for r in self.results] + self.latest
            lines += [f"Dry run: **{len(tags)}** tag(s) computed, nothing promoted", ""]
            lines += [f"- `{tag}`" for tag in tags]
            text = "\n".join(lines) + "\n\n"
            note = self._same_reference_note()
            return text + ("\n".join(note) + "\n\n" if note else "")
        statuses = [result.status for result in self.results]
        if self.settings.mode == "verify":
            lines.append(
                f"Verified **{len(statuses)}** image(s) for `{self.settings.release_tag}`: "
                f"**{statuses.count('ready')}** ready to promote, "
                f"**{statuses.count('skipped')}** already released; nothing written"
            )
        else:
            lines.append(
                f"Promoted **{statuses.count('promoted')}** and skipped "
                f"**{statuses.count('skipped')}** (already released) of "
                f"**{len(statuses)}** image(s) to `{self.settings.release_tag}`"
            )
        lines += ["", "| Name | Source | Destination | Digest | Status |"]
        lines.append("| ---- | ------ | ----------- | ------ | ------ |")
        for result in self.results:
            container = result.container
            cells = (
                container.name,
                container.source,
                container.destination,
                result.digest or "-",
                result.status,
            )
            lines.append("| " + " | ".join(gha.markdown_cell(c) for c in cells) + " |")
        if self.latest:
            moved = "Would move" if self.settings.mode == "verify" else "Moved"
            lines += ["", f"{moved} `latest`:", ""]
            lines += [f"- `{ref}`" for ref in self.latest]
        note = self._same_reference_note()
        if note:
            lines += ["", *note]
        return "\n".join(lines) + "\n\n"


def _conflict(container: Container, existing: str, source: str) -> str:
    return (
        f"{container.destination} already exists at {existing}, not the staged "
        f"{container.source} ({source}); a release tag must not change. Release "
        "under a new tag, or set on_conflict: overwrite to replace it"
    )


def _compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _crane(settings: Settings, work: str) -> Crane:
    """The registry client: installed or from PATH, logged in if asked."""
    if settings.install_crane:
        binary = install.install(work)
    else:
        found = shutil.which("crane")
        if not found:
            raise ActionError("install_crane is false but no crane is on PATH")
        binary = found
    if not settings.registry_password:
        # The runner's own logins (docker/login-action and the like).
        return Crane(binary)
    # A private docker config, deleted with the work directory: the
    # credential reaches crane for this step only, and the runner's
    # own logins (another ghcr.io token, say) are neither read nor
    # overwritten.
    config = os.path.join(work, "docker")
    os.mkdir(config, 0o700)
    crane = Crane(binary, config)
    for endpoint in settings.endpoints:
        crane.login(endpoint, settings.registry_user, settings.registry_password)
        gha.log(f"Logged in to {endpoint} as {settings.registry_user}")
    return crane


def main() -> int:
    """Run the action; returns the process exit status."""
    # Masked before anything else can print, an error message included.
    gha.mask(gha.env("INPUT_REGISTRY_PASSWORD"))
    try:
        settings = Settings.from_env()
    except ActionError as err:
        gha.annotate("error", str(err))
        return 1
    promotion = Promotion(settings, None)
    try:
        if settings.dry_run:
            ok = promotion.run()
        else:
            runner_temp = gha.env("RUNNER_TEMP") or None
            with tempfile.TemporaryDirectory(
                prefix="docker-promote-", dir=runner_temp
            ) as work:
                promotion.crane = _crane(settings, work)
                ok = promotion.run()
    except ActionError as err:
        gha.annotate("error", str(err))
        ok = False
    gha.set_outputs(promotion.outputs())
    if settings.summary:
        gha.append_summary(promotion.summary())
    return 0 if ok else 1
