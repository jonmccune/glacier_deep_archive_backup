#!/usr/bin/env bash
set -euo pipefail

MODE=$1

RESUME_FILE=state/resume_info

if [[ "$MODE" == scratch ]]; then
    SETTINGS=$(realpath "$2")
    TIMESTAMP=$(date +%Y-%m-%d-%H%M%S)
    echo "Scratch backup"
elif [[ "$MODE" == resume ]]; then
    echo "Resuming"
    if [[ ! -s "$RESUME_FILE" ]]; then
        echo "Not able to resume, please run ./backup_scratch first!"
        exit 1
    fi

    source "$RESUME_FILE"
elif [[ "$MODE" == duplicity_full ]]; then
    SETTINGS=$(realpath "$2")
    TIMESTAMP=$(date +%Y-%m-%d-%H%M%S)
    echo "Full duplicity backup"
elif [[ "$MODE" == duplicity_incremental ]]; then
    SETTINGS=$(realpath "$2")
    TIMESTAMP=$(date +%Y-%m-%d-%H%M%S)
    echo "Incremental duplicity backup"
else
    echo "Invalid mode argument: $MODE"
    exit 1
fi

# shellcheck disable=SC1090
source "$SETTINGS"

if [[ ! -s config/passphrase.txt ]]; then
    echo "Please define a passphrase in config/passphrase.txt!"
    exit 1
fi

BACKUP_MODE=${BACKUP_MODE:-files}

if [[ "$BACKUP_MODE" != files ]] && [[ "$BACKUP_MODE" != zfs_stream ]]; then
    echo "Invalid BACKUP_MODE: $BACKUP_MODE (valid: files, zfs_stream)"
    exit 1
fi

if [[ "$BACKUP_MODE" == zfs_stream ]] && [[ "$MODE" == duplicity_* ]]; then
    echo "BACKUP_MODE=zfs_stream cannot be combined with duplicity backups"
    exit 1
fi

SNAPSHOT=$ZFS_POOL@snapshot-aws-$TIMESTAMP
SET_PATH=state/sets
STATE_FILE=state/fs.state

# `zfs send -R` needs the snapshot to exist on all descendant datasets, so take and
# destroy it recursively then
SNAPSHOT_RECURSIVE=0
if [[ "$BACKUP_MODE" == zfs_stream ]]; then
    ZFS_SEND_DATASET=${ZFS_SEND_DATASET:-$ZFS_POOL}
    ZFS_SEND_SNAPSHOT=$ZFS_SEND_DATASET@snapshot-aws-$TIMESTAMP
    ZFS_SEND_RECURSIVE=${ZFS_SEND_RECURSIVE:-1}
    ZFS_SEND_EXTRA_ARGS=${ZFS_SEND_EXTRA_ARGS:-}
    # Snapshot exactly what is sent. ZFS_SEND_DATASET may be a subtree of the pool,
    # there is no point in snapshotting the rest of it.
    SNAPSHOT=$ZFS_SEND_SNAPSHOT
    SNAPSHOT_RECURSIVE=$ZFS_SEND_RECURSIVE
fi

BUFFER_PATH="$BUFFER_PATH_BASE/backup_aws_buffer"
rm -rf "$BUFFER_PATH"
mkdir -p "$BUFFER_PATH"

function cleanup()
{
    rm -rf "$BUFFER_PATH"
    if [[ "$BACKUP_MODE" == files ]]; then
        sudo umount "$SNAPSHOT_PATH" || true
    fi
    if [[ -f "$RESUME_FILE" ]]; then
        echo
        echo "Error or cancel during processing. Not destroying snapshot" \
            "($SNAPSHOT). Please check for any errors that need to be fixed" \
            "and run './backup_resume' to retry. If you do not want to" \
            "resume, please destroy the snapshot manually."
    else
        echo "Destroying snapshot $SNAPSHOT"
        if [[ "$SNAPSHOT_RECURSIVE" == 1 ]]; then
            sudo zfs destroy -r "$SNAPSHOT"
        else
            sudo zfs destroy "$SNAPSHOT"
        fi
    fi
}

if [[ "$MODE" == scratch ]] || [[ "$MODE" == duplicity_full ]] || [[ "$MODE" == duplicity_incremental ]]; then
    rm -f "$RESUME_FILE"
    rm -f "$SET_PATH"/*
    mkdir -p "$SET_PATH"
    rm -f "$STATE_FILE"

    if [[ "$SNAPSHOT_RECURSIVE" == 1 ]]; then
        sudo zfs snapshot -r "$SNAPSHOT"
    else
        sudo zfs snapshot "$SNAPSHOT"
    fi
fi

if [[ "$BACKUP_MODE" == files ]]; then
    # zfs_stream reads the snapshot with `zfs send`, which needs no mount and also
    # works for zvols, which cannot be mounted as a file system at all
    sudo mkdir -p "$SNAPSHOT_PATH"
    sudo mount -t zfs -o ro "$SNAPSHOT" "$SNAPSHOT_PATH"
fi
trap cleanup EXIT

if [[ "$MODE" == duplicity_full ]]; then
    export BUCKET_DIR BUFFER_PATH S3_BUCKET SEAL_ACTION SNAPSHOT_PATH

    impl/duplicity_backup.py full "${BACKUP_PATHS[@]}"
elif [[ "$MODE" == duplicity_incremental ]]; then
    export BUCKET_DIR BUFFER_PATH S3_BUCKET SEAL_ACTION SNAPSHOT_PATH

    impl/duplicity_backup.py incremental "${BACKUP_PATHS[@]}"
elif [[ "$BACKUP_MODE" == zfs_stream ]]; then
    export BACKUP_MODE SET_PATH SETTINGS UPLOAD_LIMIT_MB
    export ZFS_SEND_EXTRA_ARGS ZFS_SEND_RECURSIVE ZFS_SEND_SNAPSHOT

    if [[ "$MODE" == scratch ]]; then
        # There is no separate crawl step, upload_sets.py chunks the stream while it
        # is produced. Allow resuming from here on.
        echo -e "SETTINGS=\"$SETTINGS\"\\nTIMESTAMP=\"$TIMESTAMP\"" >"$RESUME_FILE"
    fi

    export BUCKET_DIR BUFFER_PATH S3_BUCKET TIMESTAMP
    impl/upload_sets.py
    rm "$RESUME_FILE"
else
    export BACKUP_MODE SET_PATH SETTINGS SNAPSHOT_PATH STATE_FILE UPLOAD_LIMIT_MB
    export SEAL_ACTION ZFS_POOL

    if [[ "$MODE" == scratch ]]; then
        impl/create_sets.py "${BACKUP_PATHS[@]}"
        echo -e "SETTINGS=\"$SETTINGS\"\\nTIMESTAMP=\"$TIMESTAMP\"" >"$RESUME_FILE"
    fi

    export BUCKET_DIR BUFFER_PATH S3_BUCKET TIMESTAMP
    impl/upload_sets.py
    rm "$RESUME_FILE"
fi


echo "Completed backup (config=$SETTINGS, timestamp=$TIMESTAMP)"
