# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Behaviour beyond the lane: checks, skips, verify mode and logins.

Defaults are proven equivalent to the lane's promotion in
test_equivalence; everything here is what the action adds.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from typing import Any
from unittest import mock

from scripts import crane, install, promote
from scripts.gha import ActionError
from scripts.refs import base_problem, login_endpoint
from tests.support import ROOT, SandboxTestCase, digest, run_action

PULL = "nexus3.example.org:10003"
PUSH = "nexus3.example.org:10002"
UNAUTHORIZED = "UNAUTHORIZED: authentication required"


def containers(*pairs: tuple[str, str]) -> str:
    return json.dumps([{"name": n, "version": v} for n, v in pairs])


def inputs(*pairs: tuple[str, str], **extra: str) -> dict[str, str]:
    return {
        "containers_json": containers(*(pairs or (("app", "1-s"),))),
        "release_tag": "1.0.0",
        "pull_registry": PULL,
        "push_registry": PUSH,
        **extra,
    }


def statuses(run: Any) -> list[str]:
    return [entry["status"] for entry in run.json("promoted")]


class ValidationTest(SandboxTestCase):
    """Every input is checked before any registry is contacted."""

    def refused(self, message: str, **overrides: str) -> None:
        run = run_action(self.sandbox, **{**inputs(), **overrides})
        self.assertEqual(run.status, 1, run.stdout)
        self.assertEqual(run.calls, [], "a registry was contacted")
        errors = [a for a in run.annotations if a.startswith("::error::")]
        self.assertEqual(len(errors), 1, run.annotations)
        self.assertIn(message, errors[0])

    def test_containers_json(self) -> None:
        cases = {
            "": "containers_json must be",
            "not json": "containers_json must be",
            "[]": "containers_json must be",
            '{"name": "a", "version": "1"}': "containers_json must be",
            '["a:1"]': "entry #1 is not an object",
            '[{"name": "a"}]': "entry a needs a string 'version'",
            # check-release normalises versions to strings; a number
            # here means the release file skipped that job.
            '[{"name": "a", "version": 1}]': "entry a needs a string 'version'",
            '[{"name": "App", "version": "1"}]': "has a 'name' that is not",
            '[{"name": "a//b", "version": "1"}]': "has a 'name' that is not",
            '[{"name": "a", "version": "-1"}]': "not a valid Docker tag",
            '[{"name": "a", "version": "1"}, {"name": "a", "version": "2"}]': (
                "names a more than once"
            ),
        }
        for raw, message in cases.items():
            with self.subTest(containers_json=raw):
                self.refused(message, containers_json=raw)

    def test_release_tag(self) -> None:
        for tag in ("", "-1", "v1 2", "a" * 129):
            with self.subTest(release_tag=tag):
                self.refused("release_tag must be a valid Docker tag", release_tag=tag)
        self.refused("'latest' is a moving tag", release_tag="latest")

    def test_registry_bases(self) -> None:
        cases = {
            "": "is required",
            # Docker would read these as Docker Hub namespaces.
            "registry/team": "not a registry host",
            "onap": "not a registry host",
            "bad-.example.org": "invalid registry host",
            "bad..example.org": "invalid registry host",
            "host.example.org/Team": "invalid repository path",
            "host.example.org//team": "invalid repository path",
            "host.example.org/team/": "ends with '/'",
            "[:::]:5000/team": "invalid registry host",
            "[deadbeef]:5000": "invalid registry host",
            "localhost:65536": "invalid registry host",
            "localhost:0": "invalid registry host",
        }
        for base, message in cases.items():
            for key in ("pull_registry", "push_registry"):
                with self.subTest(**{key: base}):
                    self.refused(message, **{key: base})

    def test_repository_path_length(self) -> None:
        self.refused(
            "over Docker's limit of 255",
            containers_json=containers(("a" * 251, "1")),
            push_registry=f"{PUSH}/team",
        )

    def test_choices_and_booleans(self) -> None:
        self.refused("mode must be one of promote, verify", mode="copy")
        self.refused("on_conflict must be one of fail, overwrite", on_conflict="skip")
        self.refused("push_latest must be 'true' or 'false'", push_latest="maybe")
        self.refused("dry_run must be 'true' or 'false'", dry_run="yes please")
        self.refused(
            "dry_run and mode: verify are exclusive", dry_run="true", mode="verify"
        )

    def test_credentials(self) -> None:
        self.refused("registry_user is empty", registry_password="pw")
        self.refused("registry_password is empty", registry_user="robot")
        self.refused(
            "registry_user may hold only",
            registry_user="robot\nx",
            registry_password="p",
        )

    def test_base_problem_accepts_what_the_lanes_use(self) -> None:
        for base in (
            "nexus3.onap.org:10003",
            "acme.jfrog.io/docker-snapshot",
            "docker-snapshot.acme.jfrog.io",
            "localhost:5000/team",
            "localhost",
            "[::1]:5000",
            "ghcr.io/org/a.b_c__d-e",
        ):
            with self.subTest(base=base):
                self.assertIsNone(base_problem(base))

    def test_login_endpoint(self) -> None:
        self.assertEqual(
            login_endpoint("acme.jfrog.io/docker-release"), "acme.jfrog.io"
        )
        self.assertEqual(
            login_endpoint("nexus3.onap.org:10002"), "nexus3.onap.org:10002"
        )
        self.assertEqual(login_endpoint("localhost:5000/a/b"), "localhost:5000")


class PromoteTest(SandboxTestCase):
    """Promotion: check everything, copy by digest, then move latest."""

    def test_promotes_by_digest_without_clobbering(self) -> None:
        staged = self.sandbox.stage(f"{PULL}/onap/app:1-s", "app")
        run = run_action(self.sandbox, **inputs(("onap/app", "1-s")))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(
            run.mutations,
            [
                [
                    "copy",
                    "--no-clobber",
                    f"{PULL}/onap/app@{staged}",
                    f"{PUSH}/onap/app:1.0.0",
                ]
            ],
        )
        self.assertEqual(
            run.json("promoted"),
            [
                {
                    "name": "onap/app",
                    "source": f"{PULL}/onap/app:1-s",
                    "destination": f"{PUSH}/onap/app:1.0.0",
                    "image": f"{PUSH}/onap/app",
                    "digest": staged,
                    "status": "promoted",
                }
            ],
        )
        self.assertEqual(
            (run.outputs["promoted_count"], run.outputs["skipped_count"]), ("1", "0")
        )
        self.assertEqual(run.outputs["latest"], "[]")

    def test_multi_architecture_index_keeps_its_digest(self) -> None:
        staged = self.sandbox.stage(f"{PULL}/app:1-s", "index", ["amd64", "arm64"])
        run = run_action(self.sandbox, **inputs())
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.tags[f"{PUSH}/app:1.0.0"], staged)
        held = run.state["manifests"][f"{PUSH}/app"]
        self.assertEqual(
            held, [staged, digest("index/amd64"), digest("index/arm64")], "children"
        )

    def test_second_run_skips(self) -> None:
        self.sandbox.stage(f"{PULL}/app:1-s", "app")
        first = run_action(self.sandbox, **inputs(push_latest="true"))
        second = run_action(self.sandbox, **inputs(push_latest="true"))
        self.assertEqual((first.status, second.status), (0, 0), second.stdout)
        self.assertEqual(statuses(second), ["skipped"])
        self.assertEqual(
            (second.outputs["promoted_count"], second.outputs["skipped_count"]),
            ("0", "1"),
        )
        # Re-running moves 'latest' again, idempotently, but copies nothing.
        self.assertEqual([call[0] for call in second.mutations], ["tag"])
        self.assertEqual(second.tags, first.tags)

    def test_conflict_fails_before_any_write(self) -> None:
        self.sandbox.stage(f"{PULL}/a:1-s", "a")
        staged_b = self.sandbox.stage(f"{PULL}/b:1-s", "b new")
        released = self.sandbox.stage(f"{PUSH}/b:1.0.0", "b old")
        run = run_action(
            self.sandbox, **inputs(("a", "1-s"), ("b", "1-s"), push_latest="true")
        )
        self.assertEqual(run.status, 1)
        self.assertEqual(run.mutations, [], "wrote despite a conflict")
        self.assertEqual(statuses(run), ["pending", "conflict"])
        self.assertEqual(run.tags[f"{PUSH}/b:1.0.0"], released)
        error = [a for a in run.annotations if a.startswith("::error::")]
        self.assertEqual(len(error), 1)
        self.assertIn(released, error[0])
        self.assertIn(staged_b, error[0])
        self.assertIn("on_conflict: overwrite", error[0])
        self.assertEqual(run.outputs["latest"], "[]")

    def test_overwrite_replaces_with_a_warning(self) -> None:
        staged = self.sandbox.stage(f"{PULL}/app:1-s", "new")
        self.sandbox.stage(f"{PUSH}/app:1.0.0", "old")
        run = run_action(self.sandbox, **inputs(on_conflict="overwrite"))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.mutations[0][:2], ["copy", f"{PULL}/app@{staged}"])
        self.assertEqual(run.tags[f"{PUSH}/app:1.0.0"], staged)
        self.assertTrue(any(a.startswith("::warning::") for a in run.annotations))
        self.assertEqual(statuses(run), ["promoted"])

    def test_every_missing_source_reported_nothing_written(self) -> None:
        self.sandbox.stage(f"{PULL}/a:1-s", "a")
        run = run_action(
            self.sandbox, **inputs(("a", "1-s"), ("b", "1-s"), ("c", "2-s"))
        )
        self.assertEqual(run.status, 1)
        self.assertEqual(run.mutations, [])
        self.assertEqual(statuses(run), ["pending", "missing", "missing"])
        errors = [a for a in run.annotations if a.startswith("::error::")]
        self.assertEqual(len(errors), 2)
        self.assertIn(f"{PULL}/c:2-s not found", errors[1])

    def test_unreadable_destination_is_not_absent(self) -> None:
        # An auth failure read as 'absent' would let the copy run over
        # whatever the tag holds.
        self.sandbox.stage(f"{PULL}/app:1-s", "app")
        self.sandbox.seed(fail={f"{PUSH}/app:1.0.0": UNAUTHORIZED})
        run = run_action(self.sandbox, **inputs())
        self.assertEqual(run.status, 1)
        self.assertEqual(run.mutations, [])
        self.assertEqual(statuses(run), ["failed"])
        self.assertTrue(any(UNAUTHORIZED in a for a in run.annotations))

    def test_unreadable_source_is_not_missing(self) -> None:
        self.sandbox.seed(fail={f"{PULL}/app": "GET: 503 Service Unavailable"})
        run = run_action(self.sandbox, **inputs())
        self.assertEqual(run.status, 1)
        self.assertEqual(statuses(run), ["failed"])
        self.assertTrue(any("503" in a for a in run.annotations))

    def test_destination_read_despite_missing_source(self) -> None:
        self.sandbox.seed(fail={f"{PUSH}/app:1.0.0": UNAUTHORIZED})
        run = run_action(self.sandbox, **inputs())
        self.assertEqual(run.status, 1)
        self.assertEqual(run.mutations, [])
        self.assertEqual(statuses(run), ["failed"])
        errors = [a for a in run.annotations if a.startswith("::error::")]
        self.assertTrue(any("not found" in e for e in errors), errors)
        self.assertTrue(any(UNAUTHORIZED in e for e in errors), errors)

    def test_destination_read_despite_unreadable_source(self) -> None:
        self.sandbox.seed(
            fail={
                f"{PULL}/app": "GET: 503 Service Unavailable",
                f"{PUSH}/app:1.0.0": UNAUTHORIZED,
            }
        )
        run = run_action(self.sandbox, **inputs())
        self.assertEqual(run.status, 1)
        self.assertEqual(statuses(run), ["failed"])
        errors = [a for a in run.annotations if a.startswith("::error::")]
        self.assertTrue(any("503" in e for e in errors), errors)
        self.assertTrue(any(UNAUTHORIZED in e for e in errors), errors)

    def test_accepts_sha384_digests(self) -> None:
        sha384 = f"sha384:{hashlib.sha384(b'app').hexdigest()}"
        self.sandbox.seed(
            tags={f"{PULL}/app:1-s": sha384}, manifests={f"{PULL}/app": [sha384]}
        )
        run = run_action(self.sandbox, **inputs(mode="verify"))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(statuses(run), ["ready"])
        self.assertEqual(run.json("promoted")[0]["digest"], sha384)

    def test_bare_404_counts_as_absent(self) -> None:
        # Registries that send no error body: crane reports the status.
        self.sandbox.stage(f"{PULL}/app:1-s", "app")
        self.sandbox.seed(
            fail={f"{PUSH}/app:1.0.0": "unexpected status code 404 Not Found: "}
        )
        run = run_action(self.sandbox, **inputs(mode="verify"))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(statuses(run), ["ready"])

    def test_404_with_a_body_is_not_absent(self) -> None:
        # crane appends an unstructured body; only an empty one is bare.
        self.sandbox.stage(f"{PULL}/app:1-s", "app")
        self.sandbox.seed(
            fail={f"{PUSH}/app:1.0.0": "unexpected status code 404 Not Found: denied"}
        )
        run = run_action(self.sandbox, **inputs())
        self.assertEqual(run.status, 1)
        self.assertEqual(run.mutations, [])
        self.assertEqual(statuses(run), ["failed"])

    def test_concurrent_identical_release_is_skipped(self) -> None:
        staged = self.sandbox.stage(f"{PULL}/app:1-s", "app")
        self.sandbox.seed(race={f"{PUSH}/app:1.0.0": staged})
        run = run_action(self.sandbox, **inputs())
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(statuses(run), ["skipped"])

    def test_concurrent_different_release_is_a_conflict(self) -> None:
        self.sandbox.stage(f"{PULL}/app:1-s", "app")
        other = digest("someone else")
        self.sandbox.seed(race={f"{PUSH}/app:1.0.0": other}, children={other: []})
        run = run_action(self.sandbox, **inputs(push_latest="true"))
        self.assertEqual(run.status, 1)
        self.assertEqual(statuses(run), ["conflict"])
        self.assertEqual(run.tags[f"{PUSH}/app:1.0.0"], other)
        self.assertNotIn(f"{PUSH}/app:latest", run.tags)

    def test_failed_copy_stops_before_latest(self) -> None:
        for name in ("a", "b", "c"):
            self.sandbox.stage(f"{PULL}/{name}:1-s", name)
        self.sandbox.seed(
            fail_copy={f"{PUSH}/b:1.0.0": "PUT: 500 Internal Server Error"}
        )
        run = run_action(
            self.sandbox,
            **inputs(("a", "1-s"), ("b", "1-s"), ("c", "1-s"), push_latest="true"),
        )
        self.assertEqual(run.status, 1)
        self.assertEqual(statuses(run), ["promoted", "failed", "pending"])
        self.assertFalse([t for t in run.tags if t.endswith(":latest")], run.tags)
        self.assertEqual(run.outputs["latest"], "[]")
        self.assertTrue(any("500 Internal Server Error" in a for a in run.annotations))

    def test_read_back_mismatch_fails(self) -> None:
        self.sandbox.stage(f"{PULL}/app:1-s", "app")
        self.sandbox.seed(after_copy={f"{PUSH}/app:1.0.0": digest("converted")})
        run = run_action(self.sandbox, **inputs(push_latest="true"))
        self.assertEqual(run.status, 1)
        self.assertEqual(statuses(run), ["failed"])
        self.assertTrue(any("reads back as" in a for a in run.annotations))
        self.assertNotIn(f"{PUSH}/app:latest", run.tags)

    def test_latest_moves_last_and_by_digest(self) -> None:
        staged = {n: self.sandbox.stage(f"{PULL}/{n}:1-s", n) for n in ("a", "b")}
        run = run_action(
            self.sandbox, **inputs(("a", "1-s"), ("b", "1-s"), push_latest="true")
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(
            [call[0] for call in run.mutations], ["copy", "copy", "tag", "tag"]
        )
        self.assertEqual(run.mutations[2], ["tag", f"{PUSH}/a@{staged['a']}", "latest"])
        self.assertEqual(run.json("latest"), [f"{PUSH}/a:latest", f"{PUSH}/b:latest"])
        for name, value in staged.items():
            self.assertEqual(run.tags[f"{PUSH}/{name}:latest"], value)

    def test_failed_latest_fails_the_step(self) -> None:
        for name in ("a", "b"):
            self.sandbox.stage(f"{PULL}/{name}:1-s", name)
        self.sandbox.seed(fail_tag={f"{PUSH}/b:latest": "PUT: 403 DENIED"})
        run = run_action(
            self.sandbox, **inputs(("a", "1-s"), ("b", "1-s"), push_latest="true")
        )
        self.assertEqual(run.status, 1)
        self.assertEqual(statuses(run), ["promoted", "promoted"])
        self.assertEqual(run.json("latest"), [f"{PUSH}/a:latest"])

    def test_rerun_with_everything_skipped_moves_latest(self) -> None:
        # A run that copied everything but failed on 'latest' leaves a
        # release that only a re-run, skipping every copy, completes.
        for name in ("a", "b"):
            self.sandbox.stage(f"{PULL}/{name}:1-s", name)
        self.sandbox.seed(fail_tag={f"{PUSH}/b:latest": "PUT: 403 DENIED"})
        pairs = (("a", "1-s"), ("b", "1-s"))
        first = run_action(self.sandbox, **inputs(*pairs, push_latest="true"))
        self.assertEqual(first.status, 1)
        self.assertNotIn(f"{PUSH}/b:latest", first.tags)
        state = self.sandbox.state()
        del state["fail_tag"]
        self.sandbox.state_file.write_text(json.dumps(state))
        second = run_action(self.sandbox, **inputs(*pairs, push_latest="true"))
        self.assertEqual(second.status, 0, second.stdout)
        self.assertEqual(statuses(second), ["skipped", "skipped"])
        self.assertEqual([call[0] for call in second.mutations], ["tag", "tag"])
        for name in ("a", "b"):
            self.assertEqual(
                second.tags[f"{PUSH}/{name}:latest"],
                second.tags[f"{PUSH}/{name}:1.0.0"],
            )

    def test_summary_table(self) -> None:
        self.sandbox.stage(f"{PULL}/app:1-s", "app")
        run = run_action(self.sandbox, **inputs(push_latest="true"))
        self.assertIn("Promoted **1** and skipped **0**", run.summary)
        self.assertIn("| app | nexus3.example.org:10003/app:1-s |", run.summary)
        self.assertIn(f"- `{PUSH}/app:latest`", run.summary)
        quiet = run_action(self.sandbox, **inputs(summary="false"))
        self.assertEqual(quiet.summary, "")

    def test_crane_output_cannot_issue_workflow_commands(self) -> None:
        self.sandbox.stage(f"{PULL}/app:1-s", "app")
        self.sandbox.seed(fail_copy={f"{PUSH}/app:1.0.0": "x\n::set-env name=A::b"})
        run = run_action(self.sandbox, **inputs())
        self.assertEqual(run.status, 1)
        # Every line the runner would execute, given ::stop-commands::.
        live, resume = [], ""
        for line in run.stdout.splitlines():
            if resume:
                resume = "" if line == resume else resume
            elif line.startswith("::stop-commands::"):
                resume = f"::{line.removeprefix('::stop-commands::')}::"
            elif line.startswith("::"):
                live.append(line)
        self.assertIn("::set-env name=A::b", run.stdout)
        self.assertFalse([line for line in live if "set-env" in line], live)
        self.assertTrue(any("Copying" in line for line in live), live)


class VerifyTest(SandboxTestCase):
    """mode: verify reads everything and writes nothing."""

    def test_reports_ready_and_skipped_without_writing(self) -> None:
        staged = self.sandbox.stage(f"{PULL}/a:1-s", "a")
        self.sandbox.stage(f"{PULL}/b:1-s", "b")
        self.sandbox.stage(f"{PUSH}/b:1.0.0", "b")
        run = run_action(
            self.sandbox,
            **inputs(("a", "1-s"), ("b", "1-s"), mode="verify", push_latest="true"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.mutations, [])
        self.assertEqual(statuses(run), ["ready", "skipped"])
        self.assertEqual(run.json("promoted")[0]["digest"], staged)
        self.assertEqual(run.json("latest"), [f"{PUSH}/a:latest", f"{PUSH}/b:latest"])
        self.assertIn("**1** ready to promote, **1** already released", run.summary)
        self.assertIn("Would move `latest`", run.summary)

    def test_reports_every_problem(self) -> None:
        self.sandbox.stage(f"{PULL}/a:1-s", "a")
        self.sandbox.stage(f"{PUSH}/a:1.0.0", "different")
        run = run_action(
            self.sandbox, **inputs(("a", "1-s"), ("b", "1-s"), mode="verify")
        )
        self.assertEqual(run.status, 1)
        self.assertEqual(run.mutations, [])
        self.assertEqual(statuses(run), ["conflict", "missing"])

    def test_overwrite_predicts_the_replacement(self) -> None:
        staged = self.sandbox.stage(f"{PULL}/a:1-s", "a")
        self.sandbox.stage(f"{PUSH}/a:1.0.0", "different")
        run = run_action(
            self.sandbox,
            **inputs(("a", "1-s"), mode="verify", on_conflict="overwrite"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.mutations, [])
        self.assertEqual(statuses(run), ["ready"])
        self.assertEqual(run.json("promoted")[0]["digest"], staged)
        warnings = [a for a in run.annotations if a.startswith("::warning::")]
        self.assertTrue(any("on_conflict: overwrite" in w for w in warnings))


class DryRunTest(SandboxTestCase):
    """dry_run contacts no registry, as the lane's does."""

    def test_no_crane_calls(self) -> None:
        run = run_action(self.sandbox, **inputs(dry_run="true", push_latest="true"))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.calls, [])
        self.assertEqual(statuses(run), ["planned"])
        self.assertEqual(run.json("promoted")[0]["digest"], "")
        self.assertEqual(run.json("latest"), [f"{PUSH}/app:latest"])

    def test_no_install_and_no_login(self) -> None:
        env = {
            "INPUT_CONTAINERS_JSON": containers(("app", "1")),
            "INPUT_RELEASE_TAG": "1.0.0",
            "INPUT_PULL_REGISTRY": PULL,
            "INPUT_PUSH_REGISTRY": PUSH,
            "INPUT_DRY_RUN": "true",
            "INPUT_INSTALL_CRANE": "true",
            "INPUT_REGISTRY_USER": "robot",
            "INPUT_REGISTRY_PASSWORD": "hunter2",
            "GITHUB_OUTPUT": "",
            "GITHUB_STEP_SUMMARY": "",
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(
                install, "install", side_effect=AssertionError("install")
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(promote.main(), 0)


class SameReferenceTest(SandboxTestCase):
    """A release file naming the release itself: skipped, with a notice."""

    def same(self, run: Any) -> list[str]:
        return [
            a
            for a in run.annotations
            if a.startswith("::notice::") and "same reference" in a
        ]

    def test_promote_skips_with_a_notice(self) -> None:
        self.sandbox.stage(f"{PUSH}/app:1.0.0", "app")
        run = run_action(
            self.sandbox,
            **inputs(("app", "1.0.0"), pull_registry=PUSH, push_latest="true"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(statuses(run), ["skipped"])
        self.assertEqual([call[0] for call in run.mutations], ["tag"])
        self.assertEqual(len(self.same(run)), 1, run.annotations)
        self.assertIn(f"{PUSH}/app:1.0.0", self.same(run)[0])
        self.assertIn("same reference", run.summary)
        self.assertIn(f"- `app`: `{PUSH}/app:1.0.0`", run.summary)

    def test_verify_skips_with_a_notice(self) -> None:
        self.sandbox.stage(f"{PUSH}/app:1.0.0", "app")
        run = run_action(
            self.sandbox,
            **inputs(("app", "1.0.0"), pull_registry=PUSH, mode="verify"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(statuses(run), ["skipped"])
        self.assertEqual(run.mutations, [])
        self.assertEqual(len(self.same(run)), 1, run.annotations)
        self.assertIn("same reference", run.summary)

    def test_dry_run_notices_offline(self) -> None:
        run = run_action(
            self.sandbox,
            **inputs(("app", "1.0.0"), pull_registry=PUSH, dry_run="true"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.calls, [])
        self.assertEqual(statuses(run), ["planned"])
        self.assertEqual(len(self.same(run)), 1, run.annotations)
        self.assertIn("same reference", run.summary)

    def test_normalised_before_comparing(self) -> None:
        cases = [
            # (pull_registry, push_registry, name, version, same)
            ("NEXUS3.Example.org:10002", PUSH, "app", "1.0.0", True),
            ("docker.io/library", "docker.io", "app", "1.0.0", True),
            ("index.docker.io", "docker.io", "onap/app", "1.0.0", True),
            ("docker.io/library", "docker.io", "onap/app", "1.0.0", False),
            ("ghcr.io/org", "ghcr.io", "app", "1.0.0", False),
            ("ghcr.io/org", "ghcr.io/org", "app", "1.0.0", True),
            (PUSH, PUSH, "app", "1.0.0-s", False),
            (PULL, PUSH, "app", "1.0.0", False),
        ]
        for pull, push, name, version, same in cases:
            with self.subTest(pull=pull, push=push, name=name, version=version):
                run = run_action(
                    self.sandbox,
                    **inputs(
                        (name, version),
                        pull_registry=pull,
                        push_registry=push,
                        dry_run="true",
                    ),
                )
                self.assertEqual(run.status, 0, run.stdout)
                self.assertEqual(len(self.same(run)), int(same), run.annotations)
                self.assertEqual("same reference" in run.summary, same)


class NamespaceTest(SandboxTestCase):
    """namespace prefixes both sides, as global-jjb's lfn_umbrella does."""

    def notices(self, run: Any, text: str) -> list[str]:
        return [a for a in run.annotations if a.startswith("::notice::") and text in a]

    def test_promotes_below_the_namespace_on_both_sides(self) -> None:
        staged = self.sandbox.stage(f"{PULL}/onap/so/adapter:1-s", "adapter")
        run = run_action(
            self.sandbox,
            **inputs(("so/adapter", "1-s"), namespace="onap", push_latest="true"),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(
            run.mutations,
            [
                [
                    "copy",
                    "--no-clobber",
                    f"{PULL}/onap/so/adapter@{staged}",
                    f"{PUSH}/onap/so/adapter:1.0.0",
                ],
                ["tag", f"{PUSH}/onap/so/adapter@{staged}", "latest"],
            ],
        )
        self.assertEqual(
            run.json("promoted"),
            [
                {
                    "name": "so/adapter",
                    "source": f"{PULL}/onap/so/adapter:1-s",
                    "destination": f"{PUSH}/onap/so/adapter:1.0.0",
                    "image": f"{PUSH}/onap/so/adapter",
                    "digest": staged,
                    "status": "promoted",
                }
            ],
        )
        self.assertEqual(run.json("latest"), [f"{PUSH}/onap/so/adapter:latest"])
        self.assertIn(
            f"| so/adapter | {PULL}/onap/so/adapter:1-s | "
            f"{PUSH}/onap/so/adapter:1.0.0 |",
            run.summary,
        )
        self.assertEqual(self.notices(run, "namespace"), [])

    def test_registry_base_paths_dry_run(self) -> None:
        pull, push = "acme.jfrog.io/docker-snapshot", "acme.jfrog.io/docker-release"
        run = run_action(
            self.sandbox,
            **inputs(
                ("app", "1-s"),
                namespace="onap/sub",
                pull_registry=pull,
                push_registry=push,
                dry_run="true",
                push_latest="true",
            ),
        )
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.calls, [])
        entry = run.json("promoted")[0]
        self.assertEqual(entry["source"], f"{pull}/onap/sub/app:1-s")
        self.assertEqual(entry["destination"], f"{push}/onap/sub/app:1.0.0")
        self.assertEqual(entry["image"], f"{push}/onap/sub/app")
        self.assertEqual(run.json("latest"), [f"{push}/onap/sub/app:latest"])
        self.assertIn(f"- `{push}/onap/sub/app:1.0.0`", run.summary)
        self.assertEqual(run.outputs["push_endpoint"], "acme.jfrog.io")

    def test_verify_reads_below_the_namespace(self) -> None:
        staged = self.sandbox.stage(f"{PULL}/onap/app:1-s", "app")
        run = run_action(self.sandbox, **inputs(namespace="onap", mode="verify"))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.mutations, [])
        self.assertEqual(statuses(run), ["ready"])
        self.assertEqual(run.json("promoted")[0]["digest"], staged)
        # Without the namespace the same staging is not found.
        bare = run_action(self.sandbox, **inputs(mode="verify"))
        self.assertEqual(statuses(bare), ["missing"])

    def test_conflict_below_the_namespace(self) -> None:
        self.sandbox.stage(f"{PULL}/onap/app:1-s", "new")
        released = self.sandbox.stage(f"{PUSH}/onap/app:1.0.0", "old")
        # The same tag outside the namespace is another repository.
        self.sandbox.stage(f"{PUSH}/app:1.0.0", "new")
        run = run_action(self.sandbox, **inputs(namespace="onap"))
        self.assertEqual(run.status, 1)
        self.assertEqual(run.mutations, [])
        self.assertEqual(statuses(run), ["conflict"])
        self.assertEqual(run.tags[f"{PUSH}/onap/app:1.0.0"], released)

    def test_skip_below_the_namespace(self) -> None:
        self.sandbox.stage(f"{PULL}/onap/app:1-s", "app")
        self.sandbox.stage(f"{PUSH}/onap/app:1.0.0", "app")
        run = run_action(self.sandbox, **inputs(namespace="onap"))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.mutations, [])
        self.assertEqual(statuses(run), ["skipped"])

    def test_same_reference_below_the_namespace(self) -> None:
        run = run_action(
            self.sandbox,
            **inputs(
                ("app", "1.0.0"), namespace="onap", pull_registry=PUSH, dry_run="true"
            ),
        )
        self.assertEqual(run.status, 0, run.stdout)
        same = self.notices(run, "same reference")
        self.assertEqual(len(same), 1, run.annotations)
        self.assertIn(f"{PUSH}/onap/app:1.0.0", same[0])
        self.assertIn(f"- `app`: `{PUSH}/onap/app:1.0.0`", run.summary)

    def test_double_prefix_kept_with_a_notice(self) -> None:
        run = run_action(
            self.sandbox,
            **inputs(
                ("onap/app", "1-s"),
                ("onapx/b", "1-s"),
                ("onap", "1-s"),
                namespace="onap",
                dry_run="true",
            ),
        )
        self.assertEqual(run.status, 0, run.stdout)
        destinations = [e["destination"] for e in run.json("promoted")]
        self.assertEqual(
            destinations,
            [
                f"{PUSH}/onap/onap/app:1.0.0",
                f"{PUSH}/onap/onapx/b:1.0.0",
                f"{PUSH}/onap/onap:1.0.0",
            ],
        )
        notices = self.notices(run, "double-prefixed")
        self.assertEqual(len(notices), 1, run.annotations)
        self.assertIn("onap/app", notices[0])
        self.assertIn(f"{PUSH}/onap/onap/app", notices[0])
        self.assertIn("relative to the namespace", notices[0])

    def test_invalid_namespaces_refused(self) -> None:
        for namespace in ("/onap", "onap/", "onap//so", "ONAP", "on ap", "-onap", "/"):
            with self.subTest(namespace=namespace):
                run = run_action(self.sandbox, **inputs(namespace=namespace))
                self.assertEqual(run.status, 1, run.stdout)
                self.assertEqual(run.calls, [])
                errors = [a for a in run.annotations if a.startswith("::error::")]
                self.assertEqual(len(errors), 1, run.annotations)
                self.assertIn("namespace", errors[0])

    def test_path_length_counts_the_namespace(self) -> None:
        name = "a" * 250
        ok = run_action(self.sandbox, **inputs((name, "1"), dry_run="true"))
        self.assertEqual(ok.status, 0, ok.stdout)
        run = run_action(
            self.sandbox, **inputs((name, "1"), namespace="onap/x", dry_run="true")
        )
        self.assertEqual(run.status, 1, run.stdout)
        self.assertIn("over Docker's limit of 255", run.stdout)

    def test_blank_namespace_adds_nothing(self) -> None:
        run = run_action(self.sandbox, **inputs(namespace=" ", dry_run="true"))
        self.assertEqual(run.status, 0, run.stdout)
        self.assertEqual(run.json("promoted")[0]["source"], f"{PULL}/app:1-s")


class LoginTest(SandboxTestCase):
    """Credentials reach crane only, for this step only."""

    def setUp(self) -> None:
        super().setUp()
        self.sandbox.stage(f"{PULL}/app:1-s", "app")
        self.sandbox.seed(
            protected={
                "nexus3.example.org:10003": "robot:s3cret",
                "nexus3.example.org:10002": "robot:s3cret",
            }
        )

    def test_without_credentials_unauthorised_fails(self) -> None:
        run = run_action(self.sandbox, **inputs())
        self.assertEqual(run.status, 1)
        self.assertEqual(statuses(run), ["failed"])
        self.assertEqual(run.mutations, [])

    def test_logs_in_to_each_endpoint_privately(self) -> None:
        ambient = self.sandbox.root / ".docker" / "config.json"
        ambient.parent.mkdir()
        caller = {
            "auths": {"ghcr.io": {"auth": base64.b64encode(b"caller:x").decode()}}
        }
        ambient.write_text(json.dumps(caller))
        run = run_action(
            self.sandbox, **inputs(registry_user="robot", registry_password="s3cret")
        )
        self.assertEqual(run.status, 0, run.stdout)
        logins = [call for call in run.calls if call[:2] == ["auth", "login"]]
        self.assertEqual(
            [call[2] for call in logins], ["nexus3.example.org:10003", PUSH]
        )
        for call in run.calls:
            self.assertNotIn("s3cret", " ".join(call), "password in argv")
        self.assertIn("::add-mask::s3cret", run.annotations)
        self.assertNotIn("s3cret", run.stdout.replace("::add-mask::s3cret", ""))
        # The caller's own logins are untouched, and the private config
        # is gone with the step.
        self.assertEqual(json.loads(ambient.read_text()), caller)
        self.assertEqual(list(self.sandbox.temp.iterdir()), [])

    def test_one_login_per_host_through_repository_paths(self) -> None:
        self.sandbox.stage("acme.jfrog.io/docker-snapshot/app:1-s", "app")
        self.sandbox.seed(protected={"acme.jfrog.io": "robot:s3cret"})
        run = run_action(
            self.sandbox,
            **inputs(
                pull_registry="acme.jfrog.io/docker-snapshot",
                push_registry="acme.jfrog.io/docker-release",
                registry_user="robot",
                registry_password="s3cret",
            ),
        )
        self.assertEqual(run.status, 0, run.stdout)
        logins = [call for call in run.calls if call[:2] == ["auth", "login"]]
        self.assertEqual([call[2] for call in logins], ["acme.jfrog.io"])
        self.assertEqual(
            (run.outputs["pull_endpoint"], run.outputs["push_endpoint"]),
            ("acme.jfrog.io", "acme.jfrog.io"),
        )
        self.assertIn("acme.jfrog.io/docker-release/app:1.0.0", run.tags)

    def test_wrong_password_fails_on_first_read(self) -> None:
        run = run_action(
            self.sandbox, **inputs(registry_user="robot", registry_password="wrong")
        )
        self.assertEqual(run.status, 1)
        self.assertEqual(run.mutations, [])
        self.assertTrue(any("UNAUTHORIZED" in a for a in run.annotations))


def _archive(binary: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("crane")
        info.size = len(binary)
        tar.addfile(info, io.BytesIO(binary))
    return buffer.getvalue()


class CraneEnvironmentTest(unittest.TestCase):
    """crane's children never see the action's inputs."""

    def test_password_input_not_inherited(self) -> None:
        secret = {"INPUT_REGISTRY_PASSWORD": "pw", "KEEP": "1"}
        with mock.patch.dict(os.environ, secret):
            env = crane.Crane("crane", "/tmp/config")._env()
        self.assertNotIn("INPUT_REGISTRY_PASSWORD", env)
        self.assertEqual(env["KEEP"], "1")
        self.assertEqual(env["DOCKER_CONFIG"], "/tmp/config")


class InstallTest(unittest.TestCase):
    """The pinned crane: refused unless it matches its recorded digest."""

    def setUp(self) -> None:
        self.archive = _archive(b"#!/bin/sh\necho crane\n")
        self.platform = mock.patch.multiple(
            install.platform, system=lambda: "Linux", machine=lambda: "x86_64"
        )
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def _fetch(self, archive: bytes) -> Any:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = archive
        return mock.patch.object(
            install.urllib.request, "urlopen", return_value=response
        )

    def test_matching_archive_installs(self) -> None:
        pinned = {("Linux", "x86_64"): hashlib.sha256(self.archive).hexdigest()}
        with (
            self._fetch(self.archive) as urlopen,
            mock.patch.dict(install.SHA256, pinned),
            tempfile.TemporaryDirectory() as directory,
        ):
            path = install.install(directory)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), b"#!/bin/sh\necho crane\n")
            self.assertTrue(os.access(path, os.X_OK))
        self.assertIn(
            "/v0.22.1/go-containerregistry_Linux_x86_64.tar.gz",
            urlopen.call_args.args[0],
        )

    def test_tampered_archive_refused(self) -> None:
        with self._fetch(self.archive), tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ActionError, "refusing to run it"):
                install.install(directory)
            self.assertEqual(os.listdir(directory), [])

    def test_unpinned_platform_refused(self) -> None:
        with (
            mock.patch.object(install.platform, "machine", return_value="s390x"),
            self.assertRaisesRegex(ActionError, "no pinned crane for Linux/s390x"),
        ):
            install.target()

    def test_every_runner_platform_pinned(self) -> None:
        self.assertEqual(
            set(install.SHA256),
            {
                ("Linux", "x86_64"),
                ("Linux", "arm64"),
                ("Darwin", "x86_64"),
                ("Darwin", "arm64"),
            },
        )
        for value in install.SHA256.values():
            self.assertRegex(value, r"^[0-9a-f]{64}$")


class ActionYamlTest(unittest.TestCase):
    """action.yaml passes every input through to the entry point."""

    def test_inputs_wired(self) -> None:
        text = (ROOT / "action.yaml").read_text()
        inputs_block = text.split("\ninputs:\n", 1)[1].split("\noutputs:\n", 1)[0]
        names = [
            line.strip().rstrip(":")
            for line in inputs_block.splitlines()
            if line.startswith("  ")
            and not line.startswith("   ")
            and line.strip()
            and not line.strip().startswith("#")
        ]
        self.assertTrue(names)
        for name in names:
            self.assertIn(f"INPUT_{name.upper()}: ${{{{ inputs.{name} }}}}", text, name)
