#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC2046
shellcheck $(git ls-files '*.sh') backup_scratch backup_resume check_progress \
    extract_archive extract_stream_archive restore test/stubs/sudo test/stubs/zfs
