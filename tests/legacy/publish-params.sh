#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Reference implementation for differential tests. DO NOT EDIT.
#
# The "Resolve registry username and login endpoints" step body from
# lfreleng-actions/docker-workflows merge.yaml (job release-publish) at
# ac1f91064b609f9be68ec5c93e1a47ba1ada8741, extracted by removing the
# ten-space YAML indent and trailing whitespace. The tests run it as the
# runner does, with bash -eo pipefail, and compare its pull_endpoint and
# push_endpoint outputs with the action's.
#
# The body below stays verbatim, so lint exceptions live in this header.
# SC2153: the upper-case names come from the lane's env: block, which
# this standalone copy cannot see.
# shellcheck disable=SC2153

# 1Password credential naming contract: the credential (and
# username) take the name of the repository being published
# (the checked-out repository, not necessarily the caller)
registry_user="${REGISTRY_USER_INPUT}"
if [ -z "${registry_user}" ] &&
  [ -n "${NEXUS_USER_INPUT}" ]; then
  echo "::warning::The nexus_user input is deprecated;" \
    "use registry_user instead"
  registry_user="${NEXUS_USER_INPUT}"
fi
if [ -z "${registry_user}" ]; then
  registry_user="${TARGET_REPOSITORY##*/}"
fi
# The credential item always keys on the published
# repository's name (registry_user overrides the login
# username only), so cross-repository calls load the
# target's item rather than the caller's
credential_name="${TARGET_REPOSITORY##*/}"
# Both values reach $GITHUB_OUTPUT, where a line break
# would append a record of its own, so each is constrained
# rather than merely escaped. The username admits '@' and
# '+' as well: registry accounts genuinely use them, since
# Artifactory SaaS identities are commonly email addresses,
# and the value only travels to docker login. The
# credential name keys a 1Password item and derives from a
# repository name, so it keeps the tighter set.
if [[ ! "${registry_user}" =~ ^[A-Za-z0-9._@+-]+$ ]]; then
  echo "::error::Invalid registry username:" \
    "${registry_user}"
  exit 1
fi
if [[ ! "${credential_name}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "::error::Invalid credential name:" \
    "${credential_name}"
  exit 1
fi
# docker login takes an endpoint, not an image reference. A
# repository path belongs to the reference and authenticates
# against the host serving it, which is how Artifactory's
# repository-path method works; Nexus 3 encodes the
# repository in the port, so there the whole value stays.
# crane reads the docker config these logins write.
pull_endpoint="${PULL_REGISTRY%%/*}"
push_endpoint="${PUSH_REGISTRY%%/*}"
echo "Pull login endpoint: ${pull_endpoint}"
echo "Push login endpoint: ${push_endpoint}"
{
  echo "registry_user=${registry_user}"
  echo "credential_name=${credential_name}"
  echo "pull_endpoint=${pull_endpoint}"
  echo "push_endpoint=${push_endpoint}"
} >> "$GITHUB_OUTPUT"
