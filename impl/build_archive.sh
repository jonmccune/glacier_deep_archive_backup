#!/usr/bin/env bash
set -euo pipefail

SNAPSHOT_PATH=$1
FILE_LIST=$2
ARCHIVE=$3
shift
shift
shift

# --compress-algo none: the data is already zstd-compressed by the time it reaches
# gpg, so gpg's own default internal compression would just burn CPU trying (and
# failing) to shrink already-high-entropy data - measured ~2.3x faster without it.
tar -C "$SNAPSHOT_PATH" --create --exclude=*/.NO_BACKUP --exclude=*/.NO_BACKUP/* \
  "$@" --verbatim-files-from "--files-from=$FILE_LIST" \
  | zstd | gpg -c --cipher-algo AES256 --compress-algo none \
    --passphrase-file config/passphrase.txt --batch \
  >"$ARCHIVE"
