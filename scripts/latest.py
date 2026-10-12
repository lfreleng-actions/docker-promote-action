# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The rule for moving 'latest': only ever to the highest release.

A release moves an image's 'latest' when its version is at least the
highest release version already in that image's repository, by SemVer
2.0.0 precedence (docker-workflows#115). Without the rule, a patch on
an older line (1.2.5 after 2.0.0) would point 'latest' back at it, and
consumers pulling 'latest' would silently downgrade a major version.

* Only tags that parse as SemVer count; 'latest', '1.2-STAGING-latest',
  'sha256-...' signature tags and the like are ignored.
* Pre-releases never move 'latest' and never count as the highest.
* Build metadata does not count, in SemVer's '+' form or the '_' form
  Docker tags carry instead: 1.2.3_build.5 equals 1.2.3.
* Equal counts as highest, so re-running the highest release converges.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

POLICIES = ("highest", "always")

# SemVer 2.0.0's grammar, from semver.org, with two allowances for
# registry tags: an optional leading 'v', and '_' before build metadata,
# since Docker tags cannot carry '+'. SemVer forbids '_' elsewhere, so
# reading it as '+' is unambiguous.
_NUMBER = r"0|[1-9][0-9]*"
_IDENTIFIER = rf"(?:{_NUMBER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_VERSION = re.compile(
    rf"v?(?P<major>{_NUMBER})\.(?P<minor>{_NUMBER})\.(?P<patch>{_NUMBER})"
    rf"(?:-(?P<prerelease>{_IDENTIFIER}(?:\.{_IDENTIFIER})*))?"
    r"(?:[+_][0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


@dataclass(frozen=True)
class Version:
    """A tag that parses as SemVer."""

    tag: str
    # Precedence between releases: build metadata plays no part.
    release: tuple[int, int, int]
    prerelease: bool


def parse(tag: str) -> Version | None:
    """``tag`` as a version, or None when it is not SemVer."""
    match = _VERSION.fullmatch(tag)
    if not match:
        return None
    release = (int(match["major"]), int(match["minor"]), int(match["patch"]))
    return Version(tag, release, match["prerelease"] is not None)


def is_release(tag: str) -> bool:
    """Whether ``tag`` is a SemVer release: neither a pre-release nor other."""
    version = parse(tag)
    return version is not None and not version.prerelease


@dataclass(frozen=True)
class Decision:
    """Whether 'latest' moves to a release, and why."""

    move: bool
    # The highest release tag already in the repository; empty when
    # there is none, or when it was not read.
    highest: str
    reason: str


ALWAYS = Decision(True, "", "latest_policy: always")
UNCHECKED = Decision(True, "", "not checked: a dry run reads no registry")


def decide(candidate: str, tags: Iterable[str]) -> Decision:
    """Whether releasing ``candidate`` moves 'latest' past ``tags``."""
    version = parse(candidate)
    if version is None or version.prerelease:
        kind = "a pre-release" if version else "not a SemVer release version"
        return Decision(False, "", f"{candidate} is {kind}; only a release moves it")
    releases = [v for v in map(parse, tags) if v is not None and not v.prerelease]
    if not releases:
        return Decision(True, "", "no release version there yet")
    highest = max(releases, key=lambda v: v.release)
    if version.release > highest.release:
        return Decision(True, highest.tag, f"newer than {highest.tag}")
    if version.release == highest.release:
        return Decision(True, highest.tag, f"equals the highest, {highest.tag}")
    return Decision(False, highest.tag, f"older than {highest.tag}")
