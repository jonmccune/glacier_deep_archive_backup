#!/usr/bin/env bash
set -euo pipefail

# Reads a raw `zfs send` stream chunk from stdin and writes it compressed and
# encrypted to ARCHIVE. The counterpart of build_archive.sh for BACKUP_MODE=zfs_stream.

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 ARCHIVE"
    echo "  Example: zfs send tank@snap | $0 /tmp/tank_00000.zfs.zstd.gpg"
    exit 1
fi

ARCHIVE=$1
# The passphrase is looked up relative to the repo root below, so make sure a
# relative archive path still refers to the caller's directory
if [[ "$ARCHIVE" != /* ]]; then
    ARCHIVE="$PWD/$ARCHIVE"
fi

pushd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" >/dev/null
trap 'popd >/dev/null' EXIT

# --compress-algo none: the data is already zstd-compressed by the time it reaches
# gpg, so gpg's own default internal compression would just burn CPU trying (and
# failing) to shrink already-high-entropy data - measured ~2.3x faster without it.
zstd | gpg -c --cipher-algo AES256 --compress-algo none \
    --passphrase-file config/passphrase.txt --batch \
  >"$ARCHIVE"
