# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The latest phase: decide on each image's 'latest', move it, report.

The rule itself is scripts/latest.py; this applies it to a run. The
decisions are made while nothing has been written yet, by listing
each image's repository, so an unreadable tag list fails the run
before any copy. Moves come last, once every image has released, and
each is decided again on a fresh listing just before it, so a higher
release another run published meanwhile keeps 'latest'. Registries
cannot swap a tag conditionally, so a short window remains between
that listing and the tag; callers releasing several versions of one
image at once should serialise them per repository.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from scripts import gha, latest
from scripts.crane import Crane
from scripts.gha import ActionError
from scripts.latest import Decision
from scripts.settings import Settings


@dataclass
class Target:
    """One image's 'latest', and the rule's decision on it."""

    image: str
    candidate: str
    digest: str
    decision: Decision
    moved: bool = False

    @property
    def ref(self) -> str:
        """The image's 'latest' reference."""
        return f"{self.image}:latest"

    def record(self) -> dict[str, object]:
        """The entry in the latest_decisions output."""
        return {
            "image": self.image,
            "digest": self.digest,
            "candidate": self.candidate,
            "highest": self.decision.highest,
            "move": self.decision.move,
            "moved": self.moved,
            "reason": self.decision.reason,
        }


class LatestPhase:
    """The rule's decisions for one run, and the moves they allow."""

    def __init__(self, settings: Settings, registry: Callable[[], Crane]) -> None:
        self.settings = settings
        self._registry = registry
        self.targets: list[Target] = []

    def announce(self) -> None:
        """Say up front when the rule cannot, or will not, apply."""
        settings = self.settings
        if not settings.moves_latest:
            return
        if settings.latest_policy == "always":
            gha.annotate(
                "warning",
                "latest_policy: always: each image's latest moves to "
                f"{settings.release_tag} without comparing it with the releases "
                "already there, so an older release or a pre-release moves it back",
            )
        elif not latest.is_release(settings.release_tag):
            reason = latest.decide(settings.release_tag, ()).reason
            gha.annotate(
                "notice",
                f"No image's latest moves: {reason} (latest_policy: highest)",
            )

    def check_pushed(self) -> bool:
        """mode: latest: each image's release tag must hold the digest given."""
        crane = self._registry()
        ok = True
        for pushed in self.settings.images:
            ref = f"{pushed.image}:{self.settings.release_tag}"
            try:
                held = crane.digest(ref)
            except ActionError as err:
                gha.annotate("error", str(err))
                ok = False
                continue
            if held == pushed.digest:
                gha.log(f"{ref} holds {held}")
                continue
            gha.annotate(
                "error",
                f"{ref} is {held or 'absent'}, not {pushed.digest}: mode: latest "
                "moves latest only to the image released under release_tag",
            )
            ok = False
        return ok

    def _decision(self, image: str) -> Decision:
        """The rule's decision for one image; reads its tags if it must."""
        settings = self.settings
        candidate = settings.release_tag
        if settings.latest_policy == "always":
            return latest.ALWAYS
        if not latest.is_release(candidate):
            return latest.decide(candidate, ())
        if settings.dry_run:
            return latest.UNCHECKED
        return latest.decide(candidate, self._registry().tags(image))

    def decide(self, subjects: Iterable[tuple[str, str]]) -> bool:
        """Decide on each (image, digest); True unless a tag list was unreadable."""
        ok = True
        for image, digest in subjects:
            try:
                decision = self._decision(image)
            except ActionError as err:
                gha.annotate("error", str(err))
                ok = False
                continue
            target = Target(image, self.settings.release_tag, digest, decision)
            self.targets.append(target)
            if decision.move:
                gha.log(
                    f"{target.ref} may move to {target.candidate}: {decision.reason}"
                )
            else:
                gha.log(f"{target.ref} stays: {decision.reason}")
        return ok

    def plan(self, subjects: Iterable[tuple[str, str]]) -> list[str]:
        """Dry run: decide, log the lane's line per move; the refs that would move."""
        self.decide(subjects)
        for target in self.targets:
            if target.decision.move:
                destination = f"{target.image}:{target.candidate}"
                gha.log(f"Dry run: would tag {destination} as latest")
        return self.movable()

    def movable(self) -> list[str]:
        """The 'latest' references the rule allows to move."""
        return [target.ref for target in self.targets if target.decision.move]

    def moved(self) -> list[str]:
        """The 'latest' references this run moved."""
        return [target.ref for target in self.targets if target.moved]

    def move(self) -> bool:
        """Point each latest the rule moves at its release digest."""
        crane = self._registry()
        for target in self.targets:
            if not target.decision.move:
                continue
            if self.settings.latest_policy != "always":
                try:
                    tags = crane.tags(target.image)
                except ActionError as err:
                    gha.annotate("error", f"Tagging {target.ref}: {err}")
                    return False
                # The release's own tag is there now; keep the check's
                # decision, and its report, unless the fresh one reverses it.
                fresh = latest.decide(target.candidate, tags)
                if not fresh.move:
                    target.decision = fresh
                    gha.annotate(
                        "notice",
                        f"{target.ref} stays: {target.decision.reason}, "
                        "released since this run checked",
                    )
                    continue
            gha.group(f"Tag {target.ref}")
            tagged = crane.tag(f"{target.image}@{target.digest}", "latest")
            gha.endgroup()
            if not tagged.ok:
                gha.annotate("error", f"Tagging {target.ref} failed: {tagged.detail}")
                return False
            try:
                landed = crane.digest(target.ref)
            except ActionError as err:
                gha.annotate("error", f"Tagging {target.ref}: {err}")
                return False
            if landed != target.digest:
                gha.annotate(
                    "error",
                    f"{target.ref} reads back as {landed or 'absent'}, "
                    f"not {target.digest}",
                )
                return False
            target.moved = True
        return True

    def records(self) -> list[dict[str, object]]:
        """The latest_decisions output."""
        return [target.record() for target in self.targets]

    def _verdict(self, target: Target) -> str:
        """What became of one latest, for the summary."""
        if target.moved:
            return "moved"
        if not target.decision.move:
            return "stays"
        # A writing run the rule allowed to move it failed first.
        return "not moved" if self.settings.writes else "would move"

    def table(self) -> list[str]:
        """The decisions as Markdown summary lines; none when there are none."""
        if not self.targets:
            return []
        lines = [
            f"`latest` (`latest_policy: {self.settings.latest_policy}`):",
            "",
            "| Image | Candidate | Highest release | latest | Reason |",
            "| ----- | --------- | --------------- | ------ | ------ |",
        ]
        for target in self.targets:
            cells = (
                target.image,
                target.candidate,
                target.decision.highest or "-",
                self._verdict(target),
                target.decision.reason,
            )
            lines.append("| " + " | ".join(gha.markdown_cell(c) for c in cells) + " |")
        return lines

    def summary(self) -> str:
        """mode: latest's step summary."""
        moved = sum(target.moved for target in self.targets)
        if self.settings.dry_run:
            outcome = "dry run, nothing read or moved"
        else:
            outcome = f"**{moved}** moved"
        lines = [
            "## Latest",
            "",
            # Given, not released: the check may just have found otherwise,
            # and a dry run reads nothing.
            f"**{len(self.settings.images)}** image(s) given for "
            f"`{self.settings.release_tag}`: {outcome}",
        ]
        table = self.table()
        if table:
            lines += ["", *table]
        return "\n".join(lines) + "\n\n"
