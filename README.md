<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 🐳 Docker Promote Action

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/docker-promote-action) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![pre-commit.ci status badge]][pre-commit.ci results page] [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/docker-promote-action/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/docker-promote-action)
<!-- prettier-ignore-end -->

Promotes staged container images to a release registry without
rebuilding them. Each staged `<pull_registry>/<name>:<version>` copies
to `<push_registry>/<name>:<release_tag>` registry-side, manifest for
manifest, so a multi-architecture manifest list keeps its digest.

## docker-promote-action

The [docker-workflows] merge lane promotes the images a container
release file names. Until now it ran `crane` inline
([docker-workflows#30]); this action replaces that step, with input
names matching the lane's so the swap is one for one. Compared with
the inline step it:

- **checks everything before writing anything.** The action reads every
  staged image and every release tag first. A missing staged image fails
  the run with nothing published, where the inline loop had already
  released the images listed before it.
- **skips releases already made.** A release tag already holding the
  staged digest reports `skipped`, as the Jenkins container release
  job does on a re-run. A release file whose staged version is the
  release tag on the release registry skips the same way, with a
  notice.
- **refuses to change a release tag.** A release tag holding a
  different digest fails the run; the inline `crane copy` replaced it
  without warning.
- **copies by digest.** The source copies by the digest the check
  resolved, and the action reads back the destination digest to compare.
- **reports digests for signing.** The `promoted` output lists each
  image and the digest the registry holds.
- **adds a verify mode that never writes**, so a verify lane can catch a
  release file naming an image that was never staged before the file
  merges.

## Usage Example

<!-- markdownlint-disable MD046 -->

```yaml
jobs:
  release-publish:
    needs: check-release
    steps:
      - id: promote
        uses: lfreleng-actions/docker-promote-action@<sha>  # vX.Y.Z
        with:
          containers_json: ${{ needs.check-release.outputs.containers_json }}
          release_tag: ${{ needs.check-release.outputs.version }}
          pull_registry: >-
            ${{ needs.check-release.outputs.pull_registry
            || inputs.snapshot_registry }}
          push_registry: >-
            ${{ needs.check-release.outputs.push_registry
            || inputs.release_registry }}
          push_latest: ${{ inputs.push_latest }}
          dry_run: ${{ inputs.dry_run }}
          registry_user: ${{ steps.publish-params.outputs.registry_user }}
          registry_password: ${{ steps.load-credential.outputs.credential }}
      # steps.promote.outputs.promoted:
      # [{"name":..., "image":..., "digest":..., "status":"promoted"}, ...]
```

### Verify a release file before it merges

```yaml
- uses: lfreleng-actions/docker-promote-action@<sha>  # vX.Y.Z
  with:
    mode: verify
    containers_json: ${{ steps.detect.outputs.containers_json }}
    release_tag: ${{ steps.detect.outputs.version }}
    pull_registry: nexus3.onap.org:10003
    push_registry: nexus3.onap.org:10002
```

<!-- markdownlint-enable MD046 -->

## Inputs

<!-- markdownlint-disable MD013 -->

| Name              | Required | Default   | Description                                                                                                                 |
| ----------------- | -------- | --------- | --------------------------------------------------------------------------------------------------------------------------- |
| containers_json   | True     |           | JSON array of `{name, version}` objects, as the check-release job emits; names are repository paths below the registry base |
| release_tag       | True     |           | Tag every image releases under; `latest` refused (see push_latest)                                                          |
| pull_registry     | True     |           | Registry base staged images come from: `host[:port]` with an optional repository path                                       |
| push_registry     | True     |           | Registry base the release goes to, in the same form                                                                         |
| push_latest       | False    | `false`   | Also point each image's `latest` at its release digest, after every image has released                                      |
| mode              | False    | `promote` | `promote`, or `verify`: read every source and destination, write nothing                                                    |
| dry_run           | False    | `false`   | Print the plan without contacting any registry                                                                              |
| on_conflict       | False    | `fail`    | A release tag holding a different digest: `fail`, or `overwrite` with a warning                                             |
| registry_user     | False    |           | Username to log in with; empty uses the runner's existing logins                                                            |
| registry_password | False    |           | Password or token for registry_user                                                                                         |
| install_crane     | False    | `true`    | Download the pinned crane and check its SHA-256; `false` uses crane from PATH                                               |
| summary           | False    | `true`    | Write a promotion report to the step summary                                                                                |

<!-- markdownlint-enable MD013 -->

## Outputs

<!-- markdownlint-disable MD013 -->

| Name           | Description                                                                           |
| -------------- | ------------------------------------------------------------------------------------- |
| promoted       | JSON list, one per container: `{name, source, destination, image, digest, status}`    |
| promoted_count | Number of images this run copied                                                      |
| skipped_count  | Number of images already released with the same digest                                |
| latest         | JSON list of `<image>:latest` references moved (verify and dry runs: that would move) |
| pull_endpoint  | Login endpoint (`host[:port]`) of pull_registry                                       |
| push_endpoint  | Login endpoint (`host[:port]`) of push_registry                                       |

<!-- markdownlint-enable MD013 -->

A `promoted` entry's `status` is one of:

<!-- markdownlint-disable MD013 -->

| Status     | Meaning                                                                                      |
| ---------- | -------------------------------------------------------------------------------------------- |
| `promoted` | Copied by this run; the release tag now holds `digest`                                       |
| `skipped`  | The release tag already held `digest`: released before                                       |
| `ready`    | `mode: verify`: promotion would copy `digest` (the tag is free, or `on_conflict: overwrite`) |
| `planned`  | `dry_run`: the action read no registry, so `digest` is empty                                 |
| `missing`  | The staged image does not exist                                                              |
| `conflict` | The release tag holds a different digest                                                     |
| `failed`   | A registry read or write failed; the step log and annotations say which                      |
| `pending`  | Not reached: another image's failure stopped the run first                                   |

<!-- markdownlint-enable MD013 -->

`digest` is the staged image's digest as the registry reports it. For
`promoted` and `skipped` entries the release tag holds it too, so
`<image>@<digest>` is the subject to sign.

## Implementation Details

### Phases

1. **Check.** For every container, read the staged digest and the
   release tag's current digest. A missing source, an unreadable
   registry or a conflicting release tag fails the run here, before
   any write, and the action reports every such problem, not merely
   the first.
2. **Copy.** Copy each source as `<repository>@<digest>`, with
   `crane copy --no-clobber`. A staged tag that moves after the check
   cannot change the release, and `--no-clobber` keeps a release tag
   another run writes meanwhile. When `--no-clobber` refuses, the
   action reads the destination again: the same digest means a
   concurrent run released the same bits (`skipped`), anything else is
   a `conflict`. After each copy the action reads back the registry's
   digest for the release tag, which must equal the staged digest. The
   first failure stops the run.
3. **Latest.** Each `latest` moves when, and not before, every image
   has released (promoted or skipped), with `crane tag
   <image>@<digest> latest`, so a failed copy never leaves a
   partially-latest release. The action reads back each `latest` too.
   `latest` moves even when every image is `skipped`: a run that
   copied everything but failed on `latest` leaves a release that a
   re-run completes, and that re-run skips every copy. The
   action does not compare the release against what `latest` held
   before, so re-running an older release moves `latest` back to it.

### Source and destination the same reference

When an entry's staged version equals the release tag and the pull
and push registry bases resolve the entry to one repository, source
and destination are one reference. Like the Jenkins container release
job, which treats an image already in the release registry under the
release tag as released, the action reports it `skipped` and copies
nothing. It also emits a `::notice::`, and lists the entry in the step
summary, so that a release file naming the release, rather than the
staged build, by mistake is visible. Every mode does this, dry runs
included: the check reads the inputs alone. The status stays `planned`
in a dry run, and verify and promote report `skipped` once the read
confirms the image exists.

The comparison normalises both references first: registry hosts
compare case-insensitively, and the Docker Hub forms `docker.io/app`,
`index.docker.io/app` and `docker.io/library/app` name one
repository. A registry base path is part of the repository, so
`ghcr.io/org` and `ghcr.io` never match each other.

### Absent and unreadable are different

`crane` exits 1 for every failure. A tag counts as absent when the
registry says so, and in no other case: `MANIFEST_UNKNOWN`,
`NAME_UNKNOWN`, or a bare
404. An `UNAUTHORIZED` response, a network error or a server error
fails the run instead. Read as absent, an unauthorised check of a
release tag would let the copy go ahead.

### Registry bases and login endpoints

A *registry base* prefixes image references and may carry a
repository path: `nexus3.onap.org:10002` (Nexus 3 selects the
repository by port) or `acme.jfrog.io/docker-release`
(Artifactory's repository-path method). A *login endpoint* is the
base's `host[:port]` alone, computed as the lanes compute it
(`${BASE%%/*}`): registries authenticate per host, never per path.
Image references use the base; logins use the endpoint, published as
`pull_endpoint` and `push_endpoint` for callers that log in
themselves.

A base must start with a registry host: a `.` or `:` in its first
component, or `localhost`. Anything else, such as `myregistry/team`,
names a Docker Hub namespace to every registry tool, so the action
refuses it.

### Logins

Given `registry_user` and `registry_password`, the action logs in to
each distinct login endpoint (one when both bases share a host) with
`crane auth login --password-stdin`, in a private docker config inside
a temporary directory that the action deletes when the step ends. The
action masks the password first, and it never appears in a command
line. The runner's own
docker config is neither read nor changed: a later step gets no
leftover credential, and an earlier `ghcr.io` login, say, is not
replaced. `crane auth login` does not check credentials, so a wrong
password surfaces as `UNAUTHORIZED` on the first read, before any
write.

Without credentials, crane uses the runner's existing docker logins,
such as `docker/login-action` steps earlier in the job.

The action logs in itself because it alone knows both bases and their
endpoints. The merge lane used to derive the endpoints in two
"Resolve registry username and login endpoint(s)" steps that had
drifted apart, then logged in twice. The release job now needs no more
than the username and the credential; the snapshot job can use the same
derivation, or this action's endpoint outputs.

### crane

The action downloads crane v0.22.1 from the go-containerregistry
release and checks the archive against the SHA-256 recorded in
`scripts/install.py` before running it, for Linux and macOS on x86_64
and arm64. The lane's `imjasonh/setup-crane` installs whichever
release is latest, unverified, and logs in to `ghcr.io` with the job
token. Set `install_crane: false` to use a crane already on PATH.

### Dry run and verify

`dry_run` keeps the lane's behaviour: no download, no login, no
registry reads, and the same log lines and step summary. The merge
lane's self-test relies on that, using registry hosts that hold none
of its images.

`mode: verify` is the non-writing counterpart of a real run: it checks
every source and destination as promotion would, fails on any
`missing` or `conflict`, and writes nothing. With `on_conflict:
overwrite` a real run would replace a release tag holding a different
digest, so verify warns and reports it `ready` instead of `conflict`.

## Compatibility

`tests/test_equivalence.py` runs the lane's "Promote staged images"
step body, vendored verbatim in `tests/legacy/`, and the action
against the same scripted registry. For each scenario it compares the
exit status and the registry left behind: every tag and every manifest
each repository holds. Dry runs also compare log lines and step
summaries. The scenarios cover releases of more than one image,
multi-architecture
indexes, Artifactory-style path bases, a failing copy (no `latest`
moves in either) and a missing image. The tests also compare the login
endpoints
against the lane's "Resolve registry username and login endpoints"
step for a set of bases.

Deliberate differences, each asserted by its own test:

- a release tag already holding a **different** digest: the lane
  overwrites it; the action fails, unless `on_conflict: overwrite`
- a **missing** staged image after others: the lane releases the
  earlier images and then fails; the action writes nothing
- a release tag already holding the **same** digest: the registry
  ends up the same, but the action copies nothing and reports
  `skipped`
- registry bases without a registry host, which the lane would pass
  to crane as Docker Hub references: the action refuses them

The Jenkins `global-jjb` container release job skips an image whose
release tag already exists, whatever it holds. The action skips when
the digests match and never otherwise; a mismatch is the case a
release must not paper over.

## Notes

Signing is out of scope. The `promoted` output carries what a signing
step needs: `image` and `digest` for each released image.

### Development

The action needs Python 3.10 or later, with no dependencies beyond the
standard library. The tests also need Bash and `jq`, which the vendored
lane scripts in `tests/legacy/` call.

```shell
python3 -m unittest discover -s tests -t .
```

`tests/fakecrane.py` stands in for crane, modelling the messages and
behaviour of crane 0.22.1 as measured against a `registry:2`
container. The CI workflow also runs the action against a real local
registry, with and without authentication.

[docker-workflows]: https://github.com/lfreleng-actions/docker-workflows
[docker-workflows#30]: https://github.com/lfreleng-actions/docker-workflows/issues/30
[pre-commit.ci results page]: https://results.pre-commit.ci/latest/github/lfreleng-actions/docker-promote-action/main
[pre-commit.ci status badge]: https://results.pre-commit.ci/badge/github/lfreleng-actions/docker-promote-action/main.svg
