# shellcheck disable=SC2034
set -euo pipefail

# The ZFS pool with data to backup
ZFS_POOL=tank

# What is backed up, see the README. Possible values:
# - files (default): The snapshot is mounted and the files below are packed with tar.
#   Supports sealing and duplicity, but cannot back up zvols.
# - zfs_stream: A `zfs send` stream of the whole pool/dataset is backed up block level,
#   including zvols, properties and all datasets. BACKUP_PATHS, SEAL_ACTION and
#   SNAPSHOT_PATH are not used in this mode, the ZFS_SEND_* settings below are.
BACKUP_MODE=files

# Files and directories to backup (recursively), relative to the ZFS pool specified above.
# These wildcards can be used:
# - * for matching any number of chars, ? for matching one char.
# - [seq] matches any character in seq, [!seq] matches any character not in seq.
# - For a literal match, wrap the meta-characters in brackets.
#   For example, '[?]' matches the character '?'.
# - ** matches all directories recursively, including the current directory.
# Matching is case sensitive.

BACKUP_PATHS=(
    "file1.txt"  # Will backup /tank/file1.txt
    "pics"  # Top-level dir, recursively
    "sports/nba"  # Subdir, recursively
    "**/?"  # All files/dirs with a filename of length 1
    "projects/**/test.py"  # All test.py files in the projects subtree
    "*"  # All files in the pool
)

# The S3 bucket where data is stored
S3_BUCKET=your_s3_bucket

# A custom directory to store all backup files of this set in.
# Inside this directory, for each scratch backup, a subdirectory named by timestamp is created.
# If left empty, timestamp directories are created at top level of the bucket.
# Do not specify a trailing slash.
BUCKET_DIR=mydata1

# The maximum size of uploaded files. Larger sizes increase the likelihood of upload
# failures and retries.
UPLOAD_LIMIT_MB=50000

# A path where the ZFS snapshot will be mounted during backup
SNAPSHOT_PATH=/snapshot_aws_backup

# Dir with at least 2 * UPLOAD_LIMIT_MB free space. A subdirectory 'backup_aws_buffer'
# will be DELETED and recreated there!
BUFFER_PATH_BASE='/tmp'

# Sealing, see the README for details. Only used with BACKUP_MODE=files.
# Possible values:
# - disable (default): Do not use sealing
# - seal_after_backup: Assume that this is the final backup of each backup path. Sets
#   each backup path to immutable on the file system and places a .GDAB_SEALED symlink.
# - skip_sealed: Do not backup any directories containing the .GDAB_SEALED marker.
SEAL_ACTION=disable

#
# The settings below are only used with BACKUP_MODE=zfs_stream.
#

# The dataset to send. Defaults to ZFS_POOL, i.e. the whole pool. Set this to back up
# only a subtree, e.g. ZFS_SEND_DATASET=tank/vms.
ZFS_SEND_DATASET=tank

# Whether to send the dataset and all its descendants (`zfs send -R`). With 1, the
# snapshot is taken recursively as well. Set to 0 to send only ZFS_SEND_DATASET
# itself.
ZFS_SEND_RECURSIVE=1

# Extra arguments for `zfs send`, split like a shell command line. Useful ones:
# - -w  Raw send. REQUIRED to back up encrypted datasets without decrypting them, and
#       the only way to send them when the key is not loaded.
# - -L  Send large blocks (needed when recordsize > 128k, otherwise the stream is
#       larger and the receiving pool needs the large_blocks feature disabled).
# - -e  More compact stream for embedded blocks.
# - -c  Send already compressed blocks as-is. Saves CPU, but zstd can then compress
#       the stream less.
ZFS_SEND_EXTRA_ARGS=''
