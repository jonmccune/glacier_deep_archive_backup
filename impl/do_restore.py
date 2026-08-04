#!/usr/bin/env python3

import json
import os
import subprocess
import time
from queue import Queue
from threading import Thread

from impl.tools import BackupException, BackupMode, size_to_string
from impl.zfs_stream import (MANIFEST_ARCHIVE_SUFFIX, ZfsReceiveStream,
                             extract_manifest_archive, extract_stream_archive,
                             make_receive_cmd, split_extra_args)

# Number of days the object stays available for download after restore.
# If there is lots of data to download, the default may have to be increased.
RESTORATION_PERIOD_DAYS = 3

NUM_DOWNLOAD_RETRIES = 3


def get_files(s3_bucket, bucket_dir, timestamp):
    prefix = f'{bucket_dir.strip("/")}/{timestamp.strip("/")}'
    cmd = ('aws', 's3api', 'list-objects-v2', '--bucket', s3_bucket, '--prefix', prefix,
           '--query', "Contents[?StorageClass=='DEEP_ARCHIVE'].[Key, Size]",
           '--no-paginate', '--output', 'json')
    cp = subprocess.run(cmd, capture_output=True, check=True)
    cp.check_returncode()
    file_list = json.loads(cp.stdout.decode())

    return file_list


def get_manifest_key(s3_bucket, bucket_dir, timestamp):
    '''Finds the stream manifest, which is in standard storage next to the chunks.'''
    prefix = f'{bucket_dir.strip("/")}/{timestamp.strip("/")}'
    query = f"Contents[?ends_with(Key, '{MANIFEST_ARCHIVE_SUFFIX}')].Key"
    cmd = ('aws', 's3api', 'list-objects-v2', '--bucket', s3_bucket, '--prefix', prefix,
           '--query', query, '--no-paginate', '--output', 'json')
    cp = subprocess.run(cmd, capture_output=True, check=True)
    cp.check_returncode()
    keys = json.loads(cp.stdout.decode()) or []
    if len(keys) != 1:
        raise BackupException(f'Expected exactly one stream manifest below {prefix} in'
                              f' bucket {s3_bucket}, found {len(keys)}: {keys}. Please'
                              ' check BUCKET_DIR/TIMESTAMP in your restore config and'
                              ' that this backup was made with BACKUP_MODE=zfs_stream.')
    return keys[0]


def request_restore(s3_bucket, file_, days, restore_tier, files_to_restore):
    restore_request = """{ "Days": %d, "GlacierJobParameters": { "Tier": "%s" } }""" \
                      % (days, restore_tier) # pylint: disable=consider-using-f-string
    print(f"Requesting restore for '{file_}', tier '{restore_tier}'")
    cmd = ('aws', 's3api', 'restore-object', '--bucket', s3_bucket, '--key', file_,
           '--restore-request', restore_request)
    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        if not 'RestoreAlreadyInProgress' in e.stderr.decode():
            raise
    files_to_restore.append(file_)


def is_restored(s3_bucket, file_):
    cmd = ('aws', 's3api', 'head-object', '--bucket', s3_bucket, '--key', file_)
    cp = subprocess.run(cmd, capture_output=True, check=True)
    cp.check_returncode()
    status = json.loads(cp.stdout.decode())
    return 'ongoing-request="false"' in status.get('Restore', '')


def download(s3_bucket, archive_path, buffer_path, label):
    bucket_path = f's3://{s3_bucket}/{archive_path}'
    cmd = ('aws', 's3', 'cp', bucket_path, buffer_path)
    for i in range(NUM_DOWNLOAD_RETRIES):
        print(f'{label}: Downloading {archive_path}, attempt {i+1}')
        try:
            subprocess.run(cmd, check=True)
            return os.path.join(buffer_path, os.path.basename(archive_path))
        except subprocess.CalledProcessError as e:
            print(f'Error during download: {e}')
    raise BackupException('Download failed, see above. Exiting.')


# Thread 1
def wait_for_restore(s3_bucket, files_to_restore):
    while len(files_to_restore) > 0:
        restored_files = []
        for file_ in files_to_restore:
            if is_restored(s3_bucket, file_):
                restored_files.append(file_)
        for restored_file in restored_files:
            files_to_restore.remove(restored_file)
            download_queue.put(restored_file)


download_in_progress = False


# Thread 2
def download_and_extract(s3_bucket, num_total_files, buffer_path, extract_path):
    global download_in_progress  # pylint: disable=global-statement
    num_processed_files = 0
    while not download_queue.empty() or len(files_to_restore) > 0:
        archive_path = download_queue.get()
        download_in_progress = True
        label = f'{num_processed_files+1}/{num_total_files}'
        archive_local_path = download(s3_bucket, archive_path, buffer_path, label)
        cmd = ('./extract_archive', archive_local_path, extract_path)
        subprocess.run(cmd, check=True)
        download_in_progress = False
        num_processed_files += 1


def print_restore_time(restore_tier):
    restore_time = {'standard': 12, 'bulk': 48}[restore_tier.lower()]
    print(f'NOTE: Restore at chosen tier {restore_tier} will take up to {restore_time}'
          ' hours')


def restore_files(s3_bucket, num_total_files, buffer_path, extract_path, restore_tier):
    os.makedirs(extract_path, exist_ok=True)

    wait_for_restore_thread = Thread(target=wait_for_restore,
                                     args=(s3_bucket, files_to_restore))
    wait_for_restore_thread.daemon = True
    wait_for_restore_thread.start()

    download_and_extract_thread = Thread(target=download_and_extract,
                                         args=(s3_bucket, num_total_files, buffer_path,
                                               extract_path))
    download_and_extract_thread.daemon = True
    download_and_extract_thread.start()

    prev_num_restores = None
    prev_num_downloads = None

    print_restore_time(restore_tier)

    while 1:
        num_restores = len(files_to_restore)
        num_downloads = download_queue.qsize()
        if num_restores != prev_num_restores or num_downloads != prev_num_downloads:
            print(f'Remaining jobs: restores={num_restores}, downloads={num_downloads}')
            if not download_in_progress and num_restores > 0:
                print('(No further output while restores are pending, please be'
                      ' patient)')
            prev_num_restores = num_restores
            prev_num_downloads = num_downloads
        if num_restores + num_downloads == 0:
            wait_for_restore_thread.join()
            download_and_extract_thread.join()
            break
        time.sleep(5)


def restore_zfs_stream(s3_bucket, manifest, buffer_path, restore_tier, recv_target,
                       recv_extra_args):
    '''Downloads the chunks in order and pipes them into a single `zfs receive`.

    All chunks together form one stream, so unlike the file mode this cannot process
    whatever happens to become available first. Restores for all chunks are requested
    up front and run in parallel on the AWS side, then each chunk is downloaded,
    decrypted, verified and fed into `zfs receive` in turn. That also keeps the buffer
    requirement at a single chunk.
    '''
    print_restore_time(restore_tier)

    cmd = make_receive_cmd(recv_target, recv_extra_args)
    num_chunks = len(manifest.chunks)
    received_bytes = 0
    with ZfsReceiveStream(cmd) as receive_stream:
        for chunk in manifest.chunks:
            index = chunk['index']
            archive_path = chunk['archive_path']
            label = f'{index+1}/{num_chunks}'
            while not is_restored(s3_bucket, archive_path):
                print(f'{label}: Waiting for restore of {archive_path}')
                time.sleep(60)
            archive_local_path = download(s3_bucket, archive_path, buffer_path, label)
            try:
                num_bytes, _ = extract_stream_archive(archive_local_path,
                                                      receive_stream.stdin,
                                                      chunk['sha256'])
            finally:
                os.unlink(archive_local_path)
            received_bytes += num_bytes
            print(f'{label}: Received {size_to_string(received_bytes)}'
                  f'/{size_to_string(manifest.uploaded_bytes())}')
        receive_stream.finish()

    print(f"Received {num_chunks} chunk(s) into '{recv_target}'")


def prepare_zfs_stream_restore(s3_bucket, bucket_dir, timestamp, files, buffer_path):
    '''Fetches the manifest and matches it against what is actually in the bucket.'''
    manifest_key = get_manifest_key(s3_bucket, bucket_dir, timestamp)
    manifest_local_path = download(s3_bucket, manifest_key, buffer_path, 'Manifest')
    try:
        manifest = extract_manifest_archive(manifest_local_path)
    finally:
        os.unlink(manifest_local_path)

    if not manifest.complete:
        print('WARNING: This backup was never completed. Only the chunks listed in the'
              ' manifest will be restored, so the received data will be truncated.')

    keys_by_name = {os.path.basename(file_[0]): file_[0] for file_ in files}
    for chunk in manifest.chunks:
        archive_name = chunk['archive_name']
        if archive_name not in keys_by_name:
            raise BackupException(f"Chunk '{archive_name}' is listed in the manifest"
                                  ' but not present in the bucket, this backup cannot'
                                  ' be restored.')
        chunk['archive_path'] = keys_by_name[archive_name]

    num_extra = len(keys_by_name) - len(manifest.chunks)
    if num_extra > 0:
        print(f'WARNING: {num_extra} archive(s) in the bucket are not listed in the'
              ' manifest and will be ignored.')

    print(f"Snapshot '{manifest.snapshot}', {len(manifest.chunks)} chunk(s),"
          f' {size_to_string(manifest.uploaded_bytes())}')
    return manifest


files_to_restore = []
download_queue = Queue()

if __name__ == '__main__':
    s3_bucket = os.environ['S3_BUCKET']
    bucket_dir = os.environ['BUCKET_DIR']
    timestamp = os.environ['TIMESTAMP']
    restore_tier = os.environ['RESTORE_TIER']
    buffer_path = os.environ['BUFFER_PATH']
    backup_mode = BackupMode()

    files = get_files(s3_bucket, bucket_dir, timestamp)
    if not files:
        raise BackupException('No files found in bucket. Please check whether the path'
                              ' specified as TIMESTAMP in your restore config exists in'
                              ' your bucket (it may contain slashes as well for'
                              ' subdirectories).')
    print(f'Found {len(files)} file(s) in bucket')

    if backup_mode.is_zfs_stream():
        recv_target = os.environ['ZFS_RECV_TARGET']
        recv_extra_args = split_extra_args(os.environ.get('ZFS_RECV_EXTRA_ARGS'))
        stream_manifest = prepare_zfs_stream_restore(s3_bucket, bucket_dir, timestamp,
                                                     files, buffer_path)
        # The chunks are processed strictly in order, so the restore requests are only
        # kicked off here, the progress bookkeeping happens in restore_zfs_stream()
        requested_chunks = []
        for chunk in stream_manifest.chunks:
            request_restore(s3_bucket, chunk['archive_path'], RESTORATION_PERIOD_DAYS,
                            restore_tier, requested_chunks)
        restore_zfs_stream(s3_bucket, stream_manifest, buffer_path, restore_tier,
                           recv_target, recv_extra_args)
    else:
        extract_path = os.environ['EXTRACT_PATH']
        for file_ in files:
            request_restore(s3_bucket, file_[0], RESTORATION_PERIOD_DAYS, restore_tier,
                            files_to_restore)
        num_total_files = len(files_to_restore)
        restore_files(s3_bucket, num_total_files, buffer_path, extract_path,
                      restore_tier)

    print('OK')
