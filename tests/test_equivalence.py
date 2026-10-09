# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Differential tests: the action against the lane's inline steps.

The release-publish job's "Promote staged images" and "Resolve
registry username and login endpoints" bodies are vendored verbatim in
tests/legacy. Each scenario runs the lane body and the action against
the same scripted crane, and compares exit status and the registry
each leaves behind: every tag, and every manifest each repository
holds. Dry runs compare the log lines and the step summary too; with
push_latest the action appends its latest decisions (below the lane's
summary, which stays verbatim), since a dry run reports them too.

The crane calls themselves differ by design: the action copies the
source by the digest its check resolved, with --no-clobber, and tags
'latest' by digest. The registry state is what callers rely on.

Five deliberate differences are asserted rather than compared:

* a release tag already holding a different digest: the lane's copy
  overwrites it; the action refuses, and reproduces the lane only
  with on_conflict: overwrite
* a missing staged image after others: the lane copies the earlier
  ones and then fails, a partial release; the action finds the gap
  before writing anything
* a release tag already holding the same digest: both leave the same
  registry, but the action skips the copy and reports 'skipped'
* a source that is the destination itself: both leave the same
  registry; the action skips it with a notice, and appends the notice
  to the lane's dry-run summary
* push_latest for a release older than one already there: the lane
  moves 'latest' back to it; the action leaves 'latest' alone, and
  reproduces the lane only with latest_policy: always
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from tests.support import Run, Sandbox, SandboxTestCase, digest, run_action, run_legacy

PULL = "nexus3.example.org:10003"
PUSH = "nexus3.example.org:10002"


def containers(*pairs: tuple[str, str]) -> str:
    return json.dumps([{"name": n, "version": v} for n, v in pairs])


Seed = Callable[[Sandbox], None]


def staged(*refs: str, multi: bool = False) -> Seed:
    def seed(sandbox: Sandbox) -> None:
        for ref in refs:
            sandbox.stage(ref, ref, ["amd64", "arm64"] if multi else [])

    return seed


def second_copy_fails(sandbox: Sandbox) -> None:
    staged(f"{PULL}/a:1-s", f"{PULL}/b:1-s", f"{PULL}/c:1-s")(sandbox)
    sandbox.seed(fail_copy={f"{PUSH}/b:1.0.0": "PUT: 500 Internal Server Error"})


# name -> (seed, inputs)
SCENARIOS: dict[str, tuple[Seed, dict[str, str]]] = {
    "one image": (
        staged(f"{PULL}/onap/app:1.0.0-20260101T000000Z"),
        {
            "containers_json": containers(("onap/app", "1.0.0-20260101T000000Z")),
            "release_tag": "1.0.0",
            "pull_registry": PULL,
            "push_registry": PUSH,
        },
    ),
    "several images with latest": (
        staged(f"{PULL}/onap/a:1.0.0-s", f"{PULL}/onap/b:2.0.0-s", f"{PULL}/c:3"),
        {
            "containers_json": containers(
                ("onap/a", "1.0.0-s"), ("onap/b", "2.0.0-s"), ("c", "3")
            ),
            "release_tag": "1.1.0",
            "pull_registry": PULL,
            "push_registry": PUSH,
            "push_latest": "true",
        },
    ),
    "multi-architecture index": (
        staged(f"{PULL}/onap/multi:1.0.0-s", multi=True),
        {
            "containers_json": containers(("onap/multi", "1.0.0-s")),
            "release_tag": "1.0.0",
            "pull_registry": PULL,
            "push_registry": PUSH,
            "push_latest": "true",
        },
    ),
    "repository-path bases (Artifactory)": (
        staged("acme.jfrog.io/docker-snapshot/team/app:0.9.0-s"),
        {
            "containers_json": containers(("team/app", "0.9.0-s")),
            "release_tag": "0.9.0",
            "pull_registry": "acme.jfrog.io/docker-snapshot",
            "push_registry": "acme.jfrog.io/docker-release",
        },
    ),
    "the second copy fails, latest untouched": (
        second_copy_fails,
        {
            "containers_json": containers(("a", "1-s"), ("b", "1-s"), ("c", "1-s")),
            "release_tag": "1.0.0",
            "pull_registry": PULL,
            "push_registry": PUSH,
            "push_latest": "true",
        },
    ),
    "the only image is missing": (
        staged(),
        {
            "containers_json": containers(("ghost", "1-s")),
            "release_tag": "1.0.0",
            "pull_registry": PULL,
            "push_registry": PUSH,
        },
    ),
}


def lane_env(inputs: Mapping[str, str]) -> dict[str, str]:
    """The promote step's env: block, from the action's inputs."""
    return {
        "CONTAINERS": inputs["containers_json"],
        "RELEASE_TAG": inputs["release_tag"],
        "PULL_REGISTRY": inputs["pull_registry"],
        "PUSH_REGISTRY": inputs["push_registry"],
        "PUSH_LATEST": inputs.get("push_latest", "false"),
        "DRY_RUN": inputs.get("dry_run", "false"),
    }


class Differential(SandboxTestCase):
    """Run both implementations from the same registry state."""

    def both(self, seed: Seed, inputs: Mapping[str, str]) -> tuple[Run, Run]:
        runs = []
        for runner in ("legacy", "action"):
            self.tearDown()
            self.setUp()
            seed(self.sandbox)
            if runner == "legacy":
                runs.append(run_legacy(self.sandbox, "promote.sh", **lane_env(inputs)))
            else:
                runs.append(run_action(self.sandbox, **inputs))
        return runs[0], runs[1]

    def assert_same_registry(self, old: Run, new: Run) -> None:
        detail = f"\n--- legacy\n{old.stdout}\n--- action\n{new.stdout}"
        self.assertEqual(new.status, old.status, "exit status" + detail)
        self.assertEqual(new.tags, old.tags, "registry tags" + detail)
        self.assertEqual(
            new.state.get("manifests"), old.state.get("manifests"), "manifests" + detail
        )


class PromotionTest(Differential):
    """Promotion against the lane's crane loop."""

    def test_same_registry_afterwards(self) -> None:
        for name, (seed, inputs) in SCENARIOS.items():
            with self.subTest(scenario=name):
                old, new = self.both(seed, inputs)
                self.assert_same_registry(old, new)

    def test_dry_run_log_and_summary(self) -> None:
        for name, (seed, inputs) in SCENARIOS.items():
            for latest in ("false", "true"):
                with self.subTest(scenario=name, push_latest=latest):
                    old, new = self.both(
                        seed, {**inputs, "dry_run": "true", "push_latest": latest}
                    )
                    self.assertEqual((old.status, new.status), (0, 0), new.stdout)
                    self.assertEqual((old.calls, new.calls), ([], []))
                    dry = [
                        line for line in new.stdout.splitlines() if "Dry run" in line
                    ]
                    self.assertEqual(dry, old.stdout.splitlines())
                    if latest == "false":
                        self.assertEqual(new.summary, old.summary)
                        continue
                    # The lane's summary, then the latest decisions.
                    self.assertTrue(new.summary.startswith(old.summary), new.summary)
                    self.assertIn(
                        "| would move |", new.summary.removeprefix(old.summary)
                    )

    def test_conflict_refused_unless_overwrite(self) -> None:
        def seed(sandbox: Sandbox) -> None:
            sandbox.stage(f"{PULL}/app:1-s", "new bits")
            sandbox.stage(f"{PUSH}/app:1.0.0", "released bits")

        inputs = {
            "containers_json": containers(("app", "1-s")),
            "release_tag": "1.0.0",
            "pull_registry": PULL,
            "push_registry": PUSH,
        }
        old, new = self.both(seed, inputs)
        self.assertEqual(old.status, 0)
        self.assertEqual(old.tags[f"{PUSH}/app:1.0.0"], digest("new bits"))
        self.assertEqual(new.status, 1)
        self.assertEqual(new.tags[f"{PUSH}/app:1.0.0"], digest("released bits"))
        self.assertEqual(new.mutations, [])
        old, new = self.both(seed, {**inputs, "on_conflict": "overwrite"})
        self.assert_same_registry(old, new)

    def test_missing_source_writes_nothing(self) -> None:
        seed = staged(f"{PULL}/a:1-s")
        inputs = {
            "containers_json": containers(("a", "1-s"), ("ghost", "1-s")),
            "release_tag": "1.0.0",
            "pull_registry": PULL,
            "push_registry": PUSH,
        }
        old, new = self.both(seed, inputs)
        self.assertEqual((old.status, new.status), (1, 1))
        # The lane released 'a' before finding 'ghost' missing.
        self.assertIn(f"{PUSH}/a:1.0.0", old.tags)
        self.assertNotIn(f"{PUSH}/a:1.0.0", new.tags)
        self.assertEqual(new.mutations, [])

    def test_already_released_is_skipped(self) -> None:
        def seed(sandbox: Sandbox) -> None:
            sandbox.stage(f"{PULL}/app:1-s", "bits")
            sandbox.stage(f"{PUSH}/app:1.0.0", "bits")

        inputs = {
            "containers_json": containers(("app", "1-s")),
            "release_tag": "1.0.0",
            "pull_registry": PULL,
            "push_registry": PUSH,
        }
        old, new = self.both(seed, inputs)
        self.assert_same_registry(old, new)
        self.assertEqual(len(old.mutations), 1)
        self.assertEqual(new.mutations, [])
        self.assertEqual(new.json("promoted")[0]["status"], "skipped")

    def test_older_release_leaves_latest_unless_always(self) -> None:
        def seed(sandbox: Sandbox) -> None:
            sandbox.stage(f"{PULL}/app:1-s", "1.2.5 bits")
            sandbox.stage(f"{PUSH}/app:2.0.0", "2.0.0 bits")
            sandbox.stage(f"{PUSH}/app:latest", "2.0.0 bits")

        inputs = {
            "containers_json": containers(("app", "1-s")),
            "release_tag": "1.2.5",
            "pull_registry": PULL,
            "push_registry": PUSH,
            "push_latest": "true",
        }
        old, new = self.both(seed, inputs)
        self.assertEqual((old.status, new.status), (0, 0), new.stdout)
        self.assertEqual(old.tags[f"{PUSH}/app:latest"], digest("1.2.5 bits"))
        self.assertEqual(new.tags[f"{PUSH}/app:latest"], digest("2.0.0 bits"))
        self.assertEqual(new.tags[f"{PUSH}/app:1.2.5"], digest("1.2.5 bits"))
        old, new = self.both(seed, {**inputs, "latest_policy": "always"})
        self.assert_same_registry(old, new)

    def test_same_reference_is_skipped_with_a_notice(self) -> None:
        # The lane copies the reference onto itself; the action skips it
        # and adds a notice, in the log and after the lane's dry-run
        # summary, so a mistaken release file is visible.
        seed = staged(f"{PUSH}/app:1.0.0")
        inputs = {
            "containers_json": containers(("app", "1.0.0")),
            "release_tag": "1.0.0",
            "pull_registry": PUSH,
            "push_registry": PUSH,
            "push_latest": "true",
        }
        old, new = self.both(seed, inputs)
        self.assert_same_registry(old, new)
        self.assertEqual(new.json("promoted")[0]["status"], "skipped")
        notices = [a for a in new.annotations if a.startswith("::notice::")]
        self.assertEqual(len(notices), 1, new.annotations)
        old, new = self.both(seed, {**inputs, "dry_run": "true"})
        dry = [line for line in new.stdout.splitlines() if "Dry run" in line]
        self.assertEqual(dry, old.stdout.splitlines())
        self.assertTrue(new.summary.startswith(old.summary), new.summary)
        self.assertIn("same reference", new.summary.removeprefix(old.summary))


# Registry bases the lanes accept and the action accepts too.
BASES = [
    "nexus3.onap.org:10003",
    "nexus3.onap.org:10002",
    "acme.jfrog.io/docker-snapshot",
    "docker-snapshot.acme.jfrog.io",
    "acme.jfrog.io/docker-release/nested/path",
    "localhost:5000",
    "localhost:5000/team",
    "localhost/team",
    "127.0.0.1:5000/a/b",
    "ghcr.io/lfreleng-actions",
    "docker.io/onap",
    "[::1]:5000/team",
]


class EndpointTest(SandboxTestCase):
    """Login endpoints against the lane's resolution step."""

    def test_endpoints_match_the_lane(self) -> None:
        for pull in BASES:
            push = BASES[(BASES.index(pull) + 3) % len(BASES)]
            with self.subTest(pull=pull, push=push):
                old = run_legacy(
                    self.sandbox,
                    "publish-params.sh",
                    REGISTRY_USER_INPUT="",
                    NEXUS_USER_INPUT="",
                    TARGET_REPOSITORY="lfreleng-actions/test",
                    PULL_REGISTRY=pull,
                    PUSH_REGISTRY=push,
                )
                new = run_action(
                    self.sandbox,
                    containers_json=containers(("app", "1")),
                    release_tag="1.0.0",
                    pull_registry=pull,
                    push_registry=push,
                    dry_run="true",
                )
                self.assertEqual((old.status, new.status), (0, 0), new.stdout)
                for key in ("pull_endpoint", "push_endpoint"):
                    self.assertEqual(new.outputs[key], old.outputs[key], key)
