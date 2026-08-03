# shellcheck disable=SC2034
set -euo pipefail

# The S3 bucket where data is stored
S3_BUCKET=your_s3_bucket

# The subdirectory that was used for backup (see your backup config)
BUCKET_DIR=mydata1

# The timestamp of the backup to restore (see your bucket for which are available)
TIMESTAMP=2022-09-14-082857

# Standard or Bulk
# Standard takes up to 12 hours to restore, Bulk up to 48 hours, Bulk is 10x cheaper
RESTORE_TIER=Bulk

# How the backup was made, must match the BACKUP_MODE of your backup config
# ('files' or 'zfs_stream'), see the README
BACKUP_MODE=files

# BACKUP_MODE=files only: Where to extract the archives to
EXTRACT_PATH=/tank_restore

# BACKUP_MODE=zfs_stream only: The dataset to receive the stream into. Existing data
# in this dataset will be DESTROYED, see the README. Use a fresh pool/dataset, not the
# one you backed up.
ZFS_RECV_TARGET=tank_restore

# BACKUP_MODE=zfs_stream only: Extra arguments for `zfs receive`. -F is required for
# the recursive (-R) streams this tool sends, -u avoids mounting the received file
# systems during restore.
ZFS_RECV_EXTRA_ARGS='-F -u'

# Dir with at least UPLOAD_LIMIT_MB free space. A subdirectory 'restore_aws_buffer'
# will be DELETED and recreated there!
BUFFER_PATH_BASE='/tmp'
