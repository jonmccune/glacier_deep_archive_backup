#!/usr/bin/env python3
'''Reports the progress of an in-progress BACKUP_MODE=zfs_stream backup.

Reads the local manifest (updated after every uploaded chunk, so this is safe to run
concurrently with a live backup) and compares it against a fresh, unprivileged size
estimate of the already-taken snapshot, to report bytes uploaded, percentage, current
rate, and estimated time remaining. Does not touch the live backup in any way - this
is a read-only status check, and deliberately needs no `sudo` access (unlike the
backup itself), so it can be run from any session, not just the one running the
backup with a cached sudo credential.
'''

import subprocess
import sys
import time
from datetime import datetime

from impl.tools import size_to_string
from impl.zfs_stream import (StreamManifest, estimate_stream_size_unprivileged,
                             make_archive_prefix)

SET_PATH = 'state/sets'


def format_duration(seconds):
    if seconds < 0:
        return 'unknown'
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f'{hours}h {minutes}m'
    if minutes:
        return f'{minutes}m {secs}s'
    return f'{secs}s'


def main():
    if len(sys.argv) != 4:
        print(f'Usage: {sys.argv[0]} SNAPSHOT TIMESTAMP RECURSIVE', file=sys.stderr)
        return 1

    snapshot, timestamp, recursive_str = sys.argv[1:4]
    recursive = recursive_str == '1'

    manifest_file = f'{SET_PATH}/{make_archive_prefix(snapshot)}.manifest.json'

    try:
        manifest = StreamManifest.load(manifest_file)
    except FileNotFoundError:
        print(f'No manifest at {manifest_file} yet - the backup is likely still'
              ' archiving its first chunk (nothing has finished uploading yet).')
        return 0

    uploaded_bytes = manifest.uploaded_bytes()
    num_chunks = len(manifest.chunks)

    start_time = datetime.strptime(timestamp, '%Y-%m-%d-%H%M%S').timestamp()
    elapsed = time.time() - start_time

    print(f'Snapshot: {snapshot}')
    print(f'Started: {timestamp} ({format_duration(elapsed)} ago)')
    print(f'Chunks uploaded so far: {num_chunks}')
    print(f'Bytes uploaded so far: {size_to_string(uploaded_bytes)}')

    rate = uploaded_bytes / elapsed if elapsed > 0 and uploaded_bytes > 0 else None
    if rate:
        print(f'Average rate since start: {size_to_string(rate)}/s')

    print()
    try:
        total_bytes = estimate_stream_size_unprivileged(snapshot, recursive=recursive)
    except subprocess.CalledProcessError as e:
        print(f'Could not estimate total size: {e.stderr}')
        return 0

    print(f'Estimated total size: {size_to_string(total_bytes)}')
    if total_bytes > 0:
        pct = 100 * uploaded_bytes / total_bytes
        print(f'Progress: {pct:.1f}%')

    if rate:
        remaining_bytes = max(total_bytes - uploaded_bytes, 0)
        eta_seconds = remaining_bytes / rate
        print(f'Estimated time remaining: {format_duration(eta_seconds)}'
              ' (based on the average rate so far - actual rate may vary)')

    return 0


if __name__ == '__main__':
    sys.exit(main())
