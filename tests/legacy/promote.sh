#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Reference implementation for differential tests. DO NOT EDIT.
#
# The "Promote staged images" step body from lfreleng-actions/docker-workflows
# merge.yaml (job release-publish) at ac1f91064b609f9be68ec5c93e1a47ba1ada8741,
# extracted by removing the ten-space YAML indent and trailing whitespace.
# The tests run it as the runner does, with bash -eo pipefail, against a
# scripted crane.
#
# The body below stays verbatim, so lint exceptions live in this header.
# SC2153: the upper-case names come from the lane's env: block, which
# this standalone copy cannot see.
# shellcheck disable=SC2153

# Registry-side promotion of every container named by the
# release file: staged name:version on the pull registry
# copies to name:release-tag on the push registry. The
# descriptor's name is the full repository path below the
# registry (the LF self-release schema, e.g.
# onap/ccsdk-odlsli-alpine-image), so no namespace applies
# here — re-adding image_namespace would double-prefix
# paths the snapshot publish already namespaced. crane
# copies manifests as-is, so multi-architecture manifest
# lists survive the promotion. Two phases: every copy
# completes before any mutable latest tag moves, so a
# failing copy never leaves a partially-latest release.
declare -a promoted=()
while IFS= read -r container; do
  name=$(jq -r '.name' <<< "${container}")
  version=$(jq -r '.version' <<< "${container}")
  src="${PULL_REGISTRY}/${name}:${version}"
  dst="${PUSH_REGISTRY}/${name}:${RELEASE_TAG}"
  if [ "${DRY_RUN}" = 'true' ]; then
    echo "Dry run: would copy ${src} -> ${dst}"
  else
    echo "::group::Promote ${src} -> ${dst}"
    crane copy "${src}" "${dst}"
    echo "::endgroup::"
  fi
  promoted+=("${dst}")
done < <(jq -c '.[]' <<< "${CONTAINERS}")
if [ "${PUSH_LATEST}" = 'true' ]; then
  while IFS= read -r name; do
    dst="${PUSH_REGISTRY}/${name}:${RELEASE_TAG}"
    if [ "${DRY_RUN}" = 'true' ]; then
      echo "Dry run: would tag ${dst} as latest"
    else
      crane tag "${dst}" latest
    fi
    promoted+=("${PUSH_REGISTRY}/${name}:latest")
  done < <(jq -r '.[] | .name' <<< "${CONTAINERS}")
fi
{
  echo "## Release Promotion"
  echo ""
  if [ "${DRY_RUN}" = 'true' ]; then
    echo "Dry run: **${#promoted[@]}** tag(s) computed," \
      "nothing promoted"
  else
    echo "Promoted **${#promoted[@]}** tag(s):"
  fi
  echo ""
  printf -- "- \`%s\`\n" "${promoted[@]}"
  echo ""
} >> "$GITHUB_STEP_SUMMARY"
