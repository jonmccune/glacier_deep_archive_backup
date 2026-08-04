#!/usr/bin/env python3

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback

from impl.tools import (BackupException, BackupMode, SealAction,
                        clean_multipart_uploads, make_set_info_filename,
                        normalize_bucket_dir, size_to_string, size_to_string_factor,
                        size_to_unit)
from impl.zfs_stream import (MANIFEST_SUFFIX, StreamManifest, ZfsSendStream,
                             build_manifest_archive, build_stream_archive,
                             estimate_stream_size, make_archive_prefix, make_send_cmd,
                             split_extra_args)

NUM_UPLOAD_RETRIES = 3

# How often the stream archiver checks whether the upload has caught up and it may
# produce the next chunk. Also bounds how long stopping it takes.
ARCHIVE_QUEUE_POLL_SEC = 1


def get_list_files(set_path):
    list_files = []

    def raise_error(error):
        raise error

    for root, _, files in os.walk(set_path, topdown=False, onerror=raise_error,
                                  followlinks=False):
        for file_ in files:
            if os.path.splitext(file_)[1] == '.list':
                list_file = os.path.join(root, file_)
                list_files.append(list_file)

    list_files.sort()

    return list_files


def build_archive(snapshot_path, list_file, buffer_path, tar_extra_args=None):
    stem = os.path.splitext(os.path.basename(list_file))[0]
    archive_name = f'{stem}.tar.zstd.gpg'
    buffer_file = os.path.join(buffer_path, archive_name)
    cmd = ['impl/build_archive.sh', snapshot_path, list_file, buffer_file]
    if tar_extra_args:
        cmd.extend(tar_extra_args)
    print(f"Running '{' '.join(cmd)}'")
    subprocess.run(cmd, check=True)

    return archive_name, buffer_file


def get_set_info_for(list_file):
    info_file = make_set_info_filename(list_file)
    with open(info_file, 'rt') as info_file:
        info = json.load(info_file)
        return info


def archiver(archive_queue, snapshot_path, list_files, buffer_path, tar_extra_args):
    archive_file = None
    list_list_filepath = None
    contents_archive_file = None
    try:
        for index, list_file in enumerate(list_files, 1):
            while archive_queue.full():
                time.sleep(5)

            print(f"Set {index}/{len(list_files)}: Packing from list '{list_file}'")

            t0 = time.time()
            archive_name, archive_file = build_archive(snapshot_path, list_file,
                                                       buffer_path, tar_extra_args)
            archive_time_sec = time.time() - t0
            archive_size_bytes = os.path.getsize(archive_file)

            info = get_set_info_for(list_file)
            archived_bytes = info['size_bytes']

            stem = os.path.basename(list_file)
            list_list_filename = f'{stem}_contents.txt'
            list_list_filepath = os.path.join(buffer_path, list_list_filename)
            with open(list_list_filepath, 'wt') as f:
                print(list_file, file=f)
            contents_archive_name, contents_archive_file = build_archive('.',
                                                                        list_list_filepath,
                                                                        buffer_path)

            archive_queue.put((list_file, archive_name, archive_file, archive_time_sec,
                               archive_size_bytes, archived_bytes, list_list_filepath,
                               contents_archive_name, contents_archive_file))
            archive_file = None
            list_list_filepath = None
            contents_archive_file = None

        archive_queue.put(True)  # All processed, success
    except:  # pylint: disable=bare-except
        if archive_file is not None:
            os.unlink(archive_file)
        if list_list_filepath is not None:
            os.unlink(list_list_filepath)
        if contents_archive_file is not None:
            os.unlink(contents_archive_file)
        archive_queue.put(False)  # Failure


class Uploader:
    def __init__(self, s3_bucket, bucket_dir, timestamp):
        self.s3_bucket = s3_bucket
        self.bucket_path_prefix = f's3://{s3_bucket}/{bucket_dir}{timestamp}'

    def __enter__(self):
        return self

    def __exit__(self, type_, value_, traceback_):
        # During upload, files will be temporarily stored in S3 standard storage.
        # Failed uploads leave orphans behind, which will cause quite high costs.
        # So drop them here.
        clean_multipart_uploads(self.s3_bucket)

    @staticmethod
    def _is_internet_reachable():
        command = ('aws', 'sts', 'get-caller-identity')
        cp = subprocess.run(command, check=False, capture_output=True, text=True)
        output = (cp.stdout + cp.stderr).lower()
        if cp.returncode == 0 or 'could not connect' not in output:
            return True
        return False

    @staticmethod
    def _wait_for_internet():
        while not Uploader._is_internet_reachable():
            print('Internet connection to AWS does not work, waiting...')
            time.sleep(5)

    def upload(self, file_, archive_name, deep_archive):
        bucket_path = f'{self.bucket_path_prefix}/{archive_name}'
        cmd = ['aws', 's3', 'cp', file_, bucket_path]
        if deep_archive:
            cmd.extend(['--storage-class', 'DEEP_ARCHIVE'])
        print(f"Running '{' '.join(cmd)}'")
        t0 = time.time()
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError:
            Uploader._wait_for_internet()
            raise
        return time.time() - t0


class ProgressPrinter():  # pylint: disable=too-many-instance-attributes
    '''Tracks archiving/upload progress and renders the status line.

    Shared by all backup modes, which only differ in how the archives are produced.
    '''
    def __init__(self, total_size_bytes):
        self.total_size_bytes = total_size_bytes
        self.archived_bytes = 0  # uncompressed
        self.archive_size_bytes = 0  # compressed
        self.gross_uploaded_bytes = 0  # uncompressed
        self.net_uploaded_bytes = 0
        self.archive_time_sec = 0
        self.upload_time_sec = 0
        self.start_time_sec = time.time()

    @staticmethod
    def _seconds_to_days(seconds):
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        comps = []
        if int(days) > 0:
            comps.append(f'{int(days)}d')
        if int(hours) > 0:
            comps.append(f'{int(hours)}h')
        comps.append(f'{int(minutes)}m')
        return ' '.join(comps)

    def print_status(self):
        elapsed_time_sec = time.time() - self.start_time_sec
        active_str = ProgressPrinter._seconds_to_days(elapsed_time_sec)

        factor, unit = size_to_unit(self.total_size_bytes)
        archived_str = (
            f'{size_to_string_factor(self.archived_bytes, factor, None)}'
            f'/{size_to_string_factor(self.total_size_bytes, factor, unit)}')
        try:
            archived_perc = 100 * self.archived_bytes / self.total_size_bytes
        except ZeroDivisionError:
            archived_perc = 100
        if self.archive_time_sec > 0:
            archived_per_sec_str = (
                f'{size_to_string(self.archived_bytes / self.archive_time_sec)}')
        else:
            archived_per_sec_str = '? MiB'
        uploaded_str = (
            f'{size_to_string_factor(self.gross_uploaded_bytes, factor, None)}'
            f'/{size_to_string_factor(self.total_size_bytes, factor, unit)}')
        try:
            upload_perc = 100 * self.gross_uploaded_bytes / self.total_size_bytes
        except ZeroDivisionError:
            upload_perc = 100
        if self.upload_time_sec > 0:
            upload_per_sec_str = (
                f'{size_to_string(self.net_uploaded_bytes / self.upload_time_sec)}')
        else:
            upload_per_sec_str = '? MiB'
        if self.archive_size_bytes > 0:
            ratio_str = f'{self.archived_bytes / self.archive_size_bytes:.1f}x'
        else:
            ratio_str = '?'
        if (self.archived_bytes > 0 and self.archive_time_sec > 0
                and self.upload_time_sec > 0 and self.gross_uploaded_bytes > 0
                and self.net_uploaded_bytes > 0):
            archived_bytes_per_sec = self.archived_bytes / self.archive_time_sec
            eta_archiving_sec = ((self.total_size_bytes - self.archived_bytes)
                                 / archived_bytes_per_sec)
            gross_remaining_upload_bytes = (self.total_size_bytes
                                            - self.gross_uploaded_bytes)
            # Pessimistic: Remaining compression is 1x
            net_uploaded_bytes_per_sec = self.net_uploaded_bytes / self.upload_time_sec
            max_eta_upload_sec = gross_remaining_upload_bytes / net_uploaded_bytes_per_sec
            # Optimistic: Compression ratio is constant as for data before
            min_eta_upload_sec = (max_eta_upload_sec *
                                  (self.net_uploaded_bytes / self.gross_uploaded_bytes))
            min_eta_str = ProgressPrinter._seconds_to_days(eta_archiving_sec
                                                           + min_eta_upload_sec)
            max_eta_str = ProgressPrinter._seconds_to_days(eta_archiving_sec
                                                           + max_eta_upload_sec)
        else:
            min_eta_str = '?'
            max_eta_str = '?'
        if min_eta_str == max_eta_str:
            eta_str = min_eta_str
        else:
            eta_str = f'{min_eta_str} - {max_eta_str}'

        msg = (f'Elapsed: {active_str}, Archived: {archived_str}'
               f' ({archived_perc:.1f}%, {archived_per_sec_str}/s)'
               f', Uploaded: {uploaded_str} ({upload_perc:.1f}%'
               f', {upload_per_sec_str}/s), Ratio: {ratio_str}, ETA: {eta_str}')

        print(msg)


def package_and_upload(snapshot_path, set_path, buffer_path, uploader, tar_extra_args):  # pylint: disable=too-many-statements
    num_errors = 0
    list_files = get_list_files(set_path)

    total_size_bytes = 0
    for list_file in list_files:
        info = get_set_info_for(list_file)
        total_size_bytes += info['size_bytes']

    progress = ProgressPrinter(total_size_bytes)

    # Upload will usually be slower than archive building. So build the archives in the
    # background, so that we will always have an archive ready for upload.
    # At most two archives will exist in parallel (one of it in process of being uploaded).
    archive_queue = queue.Queue(maxsize=1)
    archive_thread = threading.Thread(target=archiver,
                                      args=(archive_queue, snapshot_path, list_files,
                                            buffer_path, tar_extra_args))

    archive_thread.daemon = True
    archive_thread.start()

    archive_index = 0

    while True:
        result = archive_queue.get()
        if result in (True, False):
            archive_thread.join()
            if result is False:
                num_errors += 1
            break
        archive_index += 1
        (list_file, archive_name, archive_file, archive_time_sec_job,
         archive_size_bytes_job, archived_bytes_job, list_list_filepath,
         contents_archive_name, contents_archive_file) = result
        upload_success = False

        try:
            progress.archive_time_sec += archive_time_sec_job
            progress.archive_size_bytes += archive_size_bytes_job
            progress.archived_bytes += archived_bytes_job
            progress.print_status()

            for i in range(NUM_UPLOAD_RETRIES):
                print(f'Set {archive_index}/{len(list_files)}: Uploading {archive_name}'
                      f', attempt {i+1}')

                try:
                    uploader.upload(contents_archive_file, contents_archive_name,
                                    deep_archive=False)
                    file_upload_time_sec = uploader.upload(archive_file, archive_name,
                                                           deep_archive=True)

                    upload_success = True
                    progress.net_uploaded_bytes += os.path.getsize(archive_file)
                    progress.gross_uploaded_bytes += archived_bytes_job
                    progress.upload_time_sec += file_upload_time_sec
                    break
                except subprocess.CalledProcessError as e:
                    print(f'Error during upload: {e}')
                finally:
                    progress.print_status()
        finally:
            # Delete archive in any case, retry will recreate it and we need the space
            os.unlink(archive_file)
            if upload_success:
                os.unlink(list_file)
                os.unlink(make_set_info_filename(list_file))

            # We will return, clean up
            exception_pending = sys.exc_info()[0] is not None
            if upload_success or exception_pending:
                os.unlink(list_list_filepath)
                os.unlink(contents_archive_file)

            if exception_pending:
                # Clean up files in queue. This is not totally clean, since the archiver thread
                # is still running and producing, so there can be leftovers.
                result = archive_queue.get(block=False)
                if result not in (True, False):
                    archive_file = result[2]
                    list_list_filepath = result[6]
                    contents_archive_file = result[8]
                    os.unlink(archive_file)
                    os.unlink(list_list_filepath)
                    os.unlink(contents_archive_file)

            if not upload_success:
                # When upload failed, backup_resume will have to be run.
                num_errors += 1

    return num_errors


def stream_archiver(archive_queue, stream, manifest, buffer_path, num_skip_chunks):
    '''Produces the archives of a `zfs send` stream, see archiver() for the file mode.

    The first num_skip_chunks chunks were already uploaded by an interrupted run.
    `zfs send` cannot be restarted in the middle of a stream, so they are read and
    discarded, which also verifies that the stream is reproducible.

    Only reads manifest entries below num_skip_chunks, which the consumer never
    modifies, so no locking is needed.

    Returns without a result when the consumer called stream.stop(), in which case it
    is no longer interested in one.
    '''
    archive_file = None
    try:
        for index in range(num_skip_chunks):
            chunk = manifest.chunks[index]
            print(f"Chunk {index+1}/{num_skip_chunks}: Skipping {chunk['archive_name']}"
                  ', uploaded by a previous run')
            num_bytes, sha256 = stream.copy_chunk(None, manifest.chunk_size_bytes)
            if stream.aborted:
                return
            manifest.verify_chunk(index, num_bytes, sha256)

        index = num_skip_chunks
        while True:
            while archive_queue.full():
                if stream.stop_event.wait(ARCHIVE_QUEUE_POLL_SEC):
                    return

            archive_name = manifest.make_archive_name(index)
            print(f'Chunk {index+1}: Packing {archive_name}')
            archive_file = os.path.join(buffer_path, archive_name)

            t0 = time.time()
            num_bytes, sha256 = build_stream_archive(stream, archive_file,
                                                     manifest.chunk_size_bytes)
            archive_time_sec = time.time() - t0

            if num_bytes == 0 or stream.aborted:
                # The stream ended exactly at the previous chunk boundary, or the
                # consumer gave up and the partial archive is worthless
                os.unlink(archive_file)
                archive_file = None
                if stream.aborted:
                    return
                break

            archive_queue.put((index, archive_name, archive_file, archive_time_sec,
                               os.path.getsize(archive_file), num_bytes, sha256))
            archive_file = None
            index += 1

            if stream.at_eof:
                break

        stream.finish()
        archive_queue.put(True)  # All processed, success
    except:  # pylint: disable=bare-except
        traceback.print_exc()
        if archive_file is not None and os.path.exists(archive_file):
            os.unlink(archive_file)
        archive_queue.put(False)  # Failure


def drain_archive_queue(archive_queue):
    '''Removes archives the consumer will not get to, so they do not fill up the disk.'''
    while True:
        try:
            result = archive_queue.get(block=False)
        except queue.Empty:
            return
        if result in (True, False):
            continue
        archive_file = result[2]
        if os.path.exists(archive_file):
            os.unlink(archive_file)


def upload_manifest(manifest, manifest_file, buffer_path, uploader):
    '''Uploads the manifest next to the chunks, so restore can find and verify them.

    Uploaded after every chunk (it is tiny), so that even a backup that was aborted
    halfway can be restored up to the point it got to.
    '''
    archive_name = manifest.make_manifest_archive_name()
    archive_file = os.path.join(buffer_path, archive_name)
    build_manifest_archive(manifest_file, archive_file)
    try:
        for i in range(NUM_UPLOAD_RETRIES):
            print(f'Uploading manifest {archive_name}, attempt {i+1}')
            try:
                uploader.upload(archive_file, archive_name, deep_archive=False)
                return 0
            except subprocess.CalledProcessError as e:
                print(f'Error during upload: {e}')
        return 1
    finally:
        os.unlink(archive_file)


def package_and_upload_stream(stream, manifest, manifest_file, buffer_path, uploader):  # pylint: disable=too-many-statements
    num_errors = 0
    num_skip_chunks = len(manifest.chunks)

    progress = ProgressPrinter(manifest.total_size_bytes)
    # Account for what a previous, interrupted run already uploaded. The times stay at
    # zero, so rates and ETA only show up once a new chunk has been processed.
    progress.archived_bytes = manifest.uploaded_bytes()
    progress.archive_size_bytes = manifest.uploaded_archive_bytes()
    progress.gross_uploaded_bytes = progress.archived_bytes
    progress.net_uploaded_bytes = progress.archive_size_bytes

    # Build the next archive while the current one is uploaded, as for the file mode.
    # The queue size also bounds how far `zfs send` may run ahead of the upload.
    archive_queue = queue.Queue(maxsize=1)
    archive_thread = threading.Thread(target=stream_archiver,
                                      args=(archive_queue, stream, manifest, buffer_path,
                                            num_skip_chunks))

    archive_thread.daemon = True
    archive_thread.start()

    try:
        while True:
            result = archive_queue.get()
            if result in (True, False):
                if result is False:
                    num_errors += 1
                break
            (index, archive_name, archive_file, archive_time_sec_job,
             archive_size_bytes_job, chunk_size_bytes_job, sha256) = result
            upload_success = False

            try:
                progress.archive_time_sec += archive_time_sec_job
                progress.archive_size_bytes += archive_size_bytes_job
                progress.archived_bytes += chunk_size_bytes_job
                progress.print_status()

                for i in range(NUM_UPLOAD_RETRIES):
                    print(f'Chunk {index+1}: Uploading {archive_name}, attempt {i+1}')

                    try:
                        file_upload_time_sec = uploader.upload(archive_file,
                                                               archive_name,
                                                               deep_archive=True)

                        upload_success = True
                        progress.net_uploaded_bytes += archive_size_bytes_job
                        progress.gross_uploaded_bytes += chunk_size_bytes_job
                        progress.upload_time_sec += file_upload_time_sec
                        break
                    except subprocess.CalledProcessError as e:
                        print(f'Error during upload: {e}')
                    finally:
                        progress.print_status()
            finally:
                # Delete archive in any case, retry will recreate it and we need the
                # space
                os.unlink(archive_file)
                if upload_success:
                    manifest.add_chunk(index, archive_name, chunk_size_bytes_job,
                                       archive_size_bytes_job, sha256)
                    manifest.save(manifest_file)
                    num_errors += upload_manifest(manifest, manifest_file, buffer_path,
                                                  uploader)

            if not upload_success:
                # The chunks form one stream and have to be uploaded in order, so do
                # not continue with the next one. backup_resume re-runs `zfs send` and
                # skips everything uploaded so far.
                num_errors += 1
                break
    finally:
        # Whether we are done or gave up, stop the archiver and make sure it does not
        # keep filling the buffer with archives nobody will upload
        stream.stop()
        while archive_thread.is_alive():
            drain_archive_queue(archive_queue)
            archive_thread.join(timeout=1)
        drain_archive_queue(archive_queue)

    if num_errors == 0:
        manifest.complete = True
        manifest.save(manifest_file)
        num_errors += upload_manifest(manifest, manifest_file, buffer_path, uploader)

    return num_errors


def load_or_create_manifest(manifest_file, snapshot, chunk_size_bytes, recursive,
                            send_extra_args):
    '''Creates the manifest, or picks up the one left behind by an interrupted run.'''
    if not os.path.exists(manifest_file):
        total_size_bytes = estimate_stream_size(snapshot, recursive, send_extra_args)
        print(f'Estimated stream size: {size_to_string(total_size_bytes)}')
        manifest = StreamManifest(snapshot, chunk_size_bytes, total_size_bytes,
                                  recursive, send_extra_args)
        manifest.save(manifest_file)
        return manifest

    manifest = StreamManifest.load(manifest_file)
    if manifest.snapshot != snapshot:
        raise BackupException(f"Manifest '{manifest_file}' belongs to snapshot"
                              f" '{manifest.snapshot}', but '{snapshot}' should be"
                              ' backed up. Please run a scratch backup.')
    if manifest.chunk_size_bytes != chunk_size_bytes:
        print('WARNING: UPLOAD_LIMIT_MB changed since this backup was started, keeping'
              f' the chunk size of {size_to_string(manifest.chunk_size_bytes)}')
    print(f'Resuming, {len(manifest.chunks)} chunk(s) already uploaded'
          f' ({size_to_string(manifest.uploaded_bytes())})')
    return manifest


def upload_zfs_stream(set_path, buffer_path, chunk_size_bytes, uploader):
    snapshot = os.environ['ZFS_SEND_SNAPSHOT']
    recursive = os.environ.get('ZFS_SEND_RECURSIVE', '1') == '1'
    send_extra_args = split_extra_args(os.environ.get('ZFS_SEND_EXTRA_ARGS'))

    manifest_filename = f'{make_archive_prefix(snapshot)}{MANIFEST_SUFFIX}'
    manifest_file = os.path.join(set_path, manifest_filename)
    manifest = load_or_create_manifest(manifest_file, snapshot, chunk_size_bytes,
                                       recursive, send_extra_args)
    if manifest.complete:
        # All chunks are up, only uploading the restore config was left to do. No point
        # in reading the whole pool again just to find that out.
        print('All chunks were already uploaded by a previous run')
        return 0

    cmd = make_send_cmd(snapshot, manifest.recursive, manifest.send_extra_args)
    with ZfsSendStream(cmd) as stream:
        return package_and_upload_stream(stream, manifest, manifest_file, buffer_path,
                                         uploader)


def upload_restore_config(s3_bucket, bucket_dir, timestamp, settings, buffer_path,
                          uploader, template='restore.tmpl'):
    buffer_path_base = os.path.dirname(buffer_path)
    impl_path = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(impl_path, template)) as f:
        template_str = f.read()
    config = template_str.format(s3_bucket=s3_bucket, bucket_dir=bucket_dir,
                                 timestamp=timestamp, buffer_path_base=buffer_path_base)

    settings_filename = os.path.basename(settings)
    stem, ext = os.path.splitext(settings_filename)
    if stem.startswith('backup'):
        stem = stem.replace('backup', 'restore')
    else:
        stem = f'restore_{stem}'
    stem += f'_{timestamp}'
    restore_filename = stem + ext
    restore_file = os.path.join(buffer_path, restore_filename)
    with open(restore_file, 'wt') as f:
        print(config, file=f)

    for i in range(NUM_UPLOAD_RETRIES):
        print(f'Uploading restore config {restore_filename}, attempt {i+1}')
        try:
            uploader.upload(restore_file, restore_filename, deep_archive=False)
            break
        except subprocess.CalledProcessError as e:
            print(f'Error during upload: {e}')
    else:
        os.unlink(restore_file)
        return 1
    os.unlink(restore_file)
    return 0


if __name__ == '__main__':
    set_path = os.environ['SET_PATH']
    buffer_path = os.environ['BUFFER_PATH']
    s3_bucket = os.environ['S3_BUCKET']
    bucket_dir = os.environ['BUCKET_DIR']
    bucket_dir = normalize_bucket_dir(bucket_dir)
    timestamp = os.environ['TIMESTAMP']
    settings = os.environ['SETTINGS']
    upload_limit = int(os.environ['UPLOAD_LIMIT_MB']) * 1024 * 1024
    backup_mode = BackupMode()
    _, _, bytes_free = shutil.disk_usage(buffer_path)
    if bytes_free < upload_limit:
        raise BackupException(f'Not enough disk space in buffer path {buffer_path} '
                              f'(upload_limit={size_to_string(upload_limit)}, '
                              f'bytes_free={size_to_string(bytes_free)})')

    with Uploader(s3_bucket, bucket_dir, timestamp) as uploader:
        if backup_mode.is_zfs_stream():
            restore_template = 'restore_zfs_stream.tmpl'
            num_errors = upload_zfs_stream(set_path, buffer_path, upload_limit,
                                           uploader)
        else:
            restore_template = 'restore.tmpl'
            snapshot_path = os.path.normpath(os.environ['SNAPSHOT_PATH'])
            seal_action = SealAction()
            if seal_action.is_skip_sealed():
                extra_args = ('--exclude=*/.GDAB_SEALED', '--exclude=*/.GDAB_SEALED/*')
            else:
                extra_args = ()
            num_errors = package_and_upload(snapshot_path, set_path, buffer_path,
                                            uploader, extra_args)

        num_errors += upload_restore_config(s3_bucket, bucket_dir.rstrip('/'),
                                            timestamp, settings, buffer_path, uploader,
                                            restore_template)

    sys.exit(0 if num_errors == 0 else 1)
