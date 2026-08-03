'''
Block level backup of a whole ZFS pool/dataset via `zfs send`, see the README.

Used when BACKUP_MODE=zfs_stream. The `zfs send` stream is split into chunks of
UPLOAD_LIMIT_MB while it is produced, and each chunk is handed to the same
compress/encrypt/upload pipeline that the file level mode uses. The stream is never
staged on local disk in full, so the pool can be much larger than the free space of
the machine running the backup.

Restore concatenates the chunks in ascending order and pipes the result into
`zfs receive`.
'''

import hashlib
import json
import os
import shlex
import subprocess
import threading

from impl.tools import BackupException, sanitize_archive_name, size_to_string

ARCHIVE_SUFFIX = '.zfs.zstd.gpg'
MANIFEST_SUFFIX = '.manifest.json'
MANIFEST_ARCHIVE_SUFFIX = MANIFEST_SUFFIX + '.zstd.gpg'

# Zero padded chunk index in archive names. Sorting archive names lexicographically
# therefore yields the order in which the chunks have to be concatenated.
CHUNK_INDEX_DIGITS = 5

# Block size used when copying between the `zfs send`/`zfs receive` pipes and the
# archive pipeline
COPY_BLOCK_SIZE = 4 * 1024 * 1024

MANIFEST_VERSION = 1

IMPL_PATH = os.path.dirname(os.path.abspath(__file__))
ROOT_PATH = os.path.dirname(IMPL_PATH)
BUILD_STREAM_ARCHIVE = os.path.join(IMPL_PATH, 'build_stream_archive.sh')
EXTRACT_STREAM_ARCHIVE = os.path.join(ROOT_PATH, 'extract_stream_archive')


def split_extra_args(extra_args_str):
    return tuple(shlex.split(extra_args_str or ''))


def make_send_cmd(snapshot, recursive=True, extra_args=(), estimate=False):
    cmd = ['sudo', 'zfs', 'send']
    if estimate:
        # Machine parsable dry run, prints the estimated stream size
        cmd.append('-nP')
    if recursive:
        cmd.append('-R')
    cmd.extend(extra_args)
    cmd.append(snapshot)
    return cmd


def make_receive_cmd(target, extra_args=()):
    cmd = ['sudo', 'zfs', 'receive']
    cmd.extend(extra_args)
    cmd.append(target)
    return cmd


def make_archive_prefix(snapshot):
    # Only the dataset is used, the snapshot name already is part of the bucket
    # directory via the timestamp
    dataset = snapshot.split('@', maxsplit=1)[0]
    return sanitize_archive_name(dataset)


def parse_send_estimate(output):
    '''Extracts the size from `zfs send -nP` output, which ends in a `size <bytes>`.'''
    for line in reversed(output.splitlines()):
        fields = line.split()
        if len(fields) == 2 and fields[0] == 'size':
            return int(fields[1])
    return None


def estimate_stream_size(snapshot, recursive=True, extra_args=()):
    '''Returns the estimated size of the stream in bytes, for progress reporting.

    The estimate is not authoritative, the actual stream can be somewhat larger or
    smaller. It is never used to decide how much data to read.
    '''
    cmd = make_send_cmd(snapshot, recursive, extra_args, estimate=True)
    print(f"Running '{' '.join(cmd)}'")
    cp = subprocess.run(cmd, check=True, capture_output=True, text=True)
    # Depending on the ZFS version the dry run output goes to stdout or stderr
    size = parse_send_estimate(cp.stdout + cp.stderr)
    if size is None:
        raise BackupException(f"Could not determine stream size for '{snapshot}',"
                              f' output was:\n{cp.stdout}{cp.stderr}')
    return size


class StreamManifest():  # pylint: disable=too-many-instance-attributes
    '''Describes the chunks a `zfs send` stream was split into.

    A copy is kept in the state directory to allow resuming an interrupted backup, and
    an encrypted copy is uploaded next to the chunks so that restore knows how many
    chunks to expect, in which order, and what they should hash to.
    '''
    def __init__(self, snapshot, chunk_size_bytes, total_size_bytes, recursive=True,
                 send_extra_args=()):
        self.snapshot = snapshot
        self.chunk_size_bytes = chunk_size_bytes
        self.total_size_bytes = total_size_bytes
        self.recursive = recursive
        self.send_extra_args = list(send_extra_args)
        self.archive_prefix = make_archive_prefix(snapshot)
        self.complete = False
        self.chunks = []

    def make_archive_name(self, index):
        return f'{self.archive_prefix}_{index:0{CHUNK_INDEX_DIGITS}d}{ARCHIVE_SUFFIX}'

    def make_manifest_archive_name(self):
        return f'{self.archive_prefix}{MANIFEST_ARCHIVE_SUFFIX}'

    def add_chunk(self, index, archive_name, size_bytes, archive_size_bytes, sha256):
        if index != len(self.chunks):
            raise BackupException(f'Chunks must be added in order, got index {index},'
                                  f' expected {len(self.chunks)}')
        self.chunks.append({'index': index, 'archive_name': archive_name,
                            'size_bytes': size_bytes,
                            'archive_size_bytes': archive_size_bytes,
                            'sha256': sha256})

    def verify_chunk(self, index, size_bytes, sha256):
        '''Checks re-read stream data against what was uploaded before.

        `zfs send` is re-run from the start when resuming, and the already uploaded
        prefix of the stream is read and discarded. ZFS does not guarantee that two
        runs produce a byte identical stream, so verify it instead of silently
        creating a backup that cannot be received.
        '''
        chunk = self.chunks[index]
        if size_bytes == chunk['size_bytes'] and sha256 == chunk['sha256']:
            return
        msg = (f"Chunk {index} of snapshot '{self.snapshot}' differs from the chunk"
               ' uploaded before (uploaded:'
               f" {size_to_string(chunk['size_bytes'])}/{chunk['sha256']}"
               f', now: {size_to_string(size_bytes)}/{sha256}).'
               ' The `zfs send` stream is not reproducible, so this backup cannot be'
               ' resumed. Please delete the backup directory in your bucket and start'
               ' a scratch backup.')
        raise BackupException(msg)

    def uploaded_bytes(self):
        return sum(chunk['size_bytes'] for chunk in self.chunks)

    def uploaded_archive_bytes(self):
        return sum(chunk['archive_size_bytes'] for chunk in self.chunks)

    def to_dict(self):
        return {'version': MANIFEST_VERSION, 'snapshot': self.snapshot,
                'recursive': self.recursive, 'send_extra_args': self.send_extra_args,
                'archive_prefix': self.archive_prefix,
                'chunk_size_bytes': self.chunk_size_bytes,
                'total_size_bytes': self.total_size_bytes, 'complete': self.complete,
                'chunks': self.chunks}

    @staticmethod
    def from_dict(info):
        version = info.get('version')
        if version != MANIFEST_VERSION:
            raise BackupException(f'Unsupported manifest version {version}, this'
                                  f' version of GDAB writes {MANIFEST_VERSION}')
        manifest = StreamManifest(info['snapshot'], info['chunk_size_bytes'],
                                  info['total_size_bytes'], info['recursive'],
                                  info['send_extra_args'])
        manifest.archive_prefix = info['archive_prefix']
        manifest.complete = info['complete']
        manifest.chunks = info['chunks']
        return manifest

    def save(self, manifest_file):
        # Written after every uploaded chunk and used to resume, so make sure a crash
        # cannot leave a truncated file behind
        tmp_file = f'{manifest_file}.tmp'
        with open(tmp_file, 'wt') as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp_file, manifest_file)

    @staticmethod
    def load(manifest_file):
        with open(manifest_file, 'rt') as f:
            return StreamManifest.from_dict(json.load(f))


class ZfsSendStream():
    '''Reads a `zfs send` stream chunk by chunk.

    The command is passed in so that it can be replaced in tests.
    '''
    def __init__(self, cmd):
        self.cmd = list(cmd)
        self.process = None
        self.at_eof = False
        self.aborted = False
        self.bytes_read = 0
        # A chunk of UPLOAD_LIMIT_MB takes a while to read, so allow the consumer to
        # abort in the middle of one instead of waiting for it to complete
        self.stop_event = threading.Event()

    def __enter__(self):
        print(f"Running '{' '.join(self.cmd)}'")
        # pylint: disable=consider-using-with
        self.process = subprocess.Popen(self.cmd, stdout=subprocess.PIPE)
        return self

    def stop(self):
        '''Makes the next/current copy_chunk() return early.'''
        self.stop_event.set()

    def __exit__(self, type_, value_, traceback_):
        if self.process.poll() is None:
            # Not fully consumed, e.g. because of an error further down the pipeline.
            # Do not leave a `zfs send` running that nobody reads from.
            self.process.kill()
        self.process.stdout.close()
        self.process.wait()
        return False

    def copy_chunk(self, dst, max_bytes):
        '''Copies up to max_bytes from the stream to dst (None discards the data).

        Returns the number of bytes copied and their sha256. Copying fewer bytes than
        requested means the end of the stream was reached, or stop() was called, which
        is indicated by at_eof/aborted.
        '''
        hasher = hashlib.sha256()
        remaining = max_bytes
        while remaining > 0:
            if self.stop_event.is_set():
                self.aborted = True
                break
            block = self.process.stdout.read(min(COPY_BLOCK_SIZE, remaining))
            if not block:
                self.at_eof = True
                break
            hasher.update(block)
            if dst is not None:
                dst.write(block)
            remaining -= len(block)
        num_bytes = max_bytes - remaining
        self.bytes_read += num_bytes
        return num_bytes, hasher.hexdigest()

    def finish(self):
        '''Waits for `zfs send` to exit and raises when it failed.'''
        self.process.stdout.close()
        returncode = self.process.wait()
        if returncode != 0:
            raise BackupException(f"'{' '.join(self.cmd)}' failed with exit code"
                                  f' {returncode}')


class ZfsReceiveStream():
    '''Feeds a reassembled `zfs send` stream into `zfs receive`.

    One process is kept open for the whole restore, since all chunks together form a
    single stream. The command is passed in so that it can be replaced in tests.
    '''
    def __init__(self, cmd):
        self.cmd = list(cmd)
        self.process = None

    def __enter__(self):
        print(f"Running '{' '.join(self.cmd)}'")
        # pylint: disable=consider-using-with
        self.process = subprocess.Popen(self.cmd, stdin=subprocess.PIPE)
        return self

    def __exit__(self, type_, value_, traceback_):
        if self.process.poll() is None:
            self.process.kill()
        if not self.process.stdin.closed:
            self.process.stdin.close()
        self.process.wait()
        return False

    @property
    def stdin(self):
        return self.process.stdin

    def finish(self):
        self.process.stdin.close()
        returncode = self.process.wait()
        if returncode != 0:
            raise BackupException(f"'{' '.join(self.cmd)}' failed with exit code"
                                  f' {returncode}')


def build_stream_archive(stream, archive_file, chunk_size_bytes):
    '''Compresses and encrypts the next chunk of the stream into archive_file.

    Returns the number of uncompressed bytes and their sha256. A result of zero bytes
    means the stream ended exactly at the previous chunk boundary, in which case
    archive_file holds no payload and must not be uploaded.
    '''
    cmd = [BUILD_STREAM_ARCHIVE, archive_file]
    print(f"Running '{' '.join(cmd)}'")
    with subprocess.Popen(cmd, stdin=subprocess.PIPE) as archive_process:
        num_bytes, sha256 = stream.copy_chunk(archive_process.stdin, chunk_size_bytes)
        archive_process.stdin.close()
    if archive_process.returncode != 0:
        raise BackupException(f"'{' '.join(cmd)}' failed with exit code"
                              f' {archive_process.returncode}')
    return num_bytes, sha256


def build_manifest_archive(manifest_file, archive_file):
    cmd = [BUILD_STREAM_ARCHIVE, archive_file]
    print(f"Running '{' '.join(cmd)}'")
    with open(manifest_file, 'rb') as f:
        subprocess.run(cmd, stdin=f, check=True)


def extract_stream_archive(archive_file, dst, expected_sha256=None):
    '''Decrypts/decompresses one chunk into dst, verifying it against the manifest.'''
    cmd = [EXTRACT_STREAM_ARCHIVE, archive_file]
    print(f"Running '{' '.join(cmd)}'")
    hasher = hashlib.sha256()
    num_bytes = 0
    with subprocess.Popen(cmd, stdout=subprocess.PIPE) as extract_process:
        while True:
            block = extract_process.stdout.read(COPY_BLOCK_SIZE)
            if not block:
                break
            hasher.update(block)
            dst.write(block)
            num_bytes += len(block)
    if extract_process.returncode != 0:
        raise BackupException(f"'{' '.join(cmd)}' failed with exit code"
                              f' {extract_process.returncode}')
    sha256 = hasher.hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise BackupException(f"Chunk '{archive_file}' is corrupt: expected sha256"
                              f' {expected_sha256}, got {sha256}')
    return num_bytes, sha256


def extract_manifest_archive(archive_file):
    cmd = [EXTRACT_STREAM_ARCHIVE, archive_file]
    print(f"Running '{' '.join(cmd)}'")
    cp = subprocess.run(cmd, check=True, capture_output=True)
    return StreamManifest.from_dict(json.loads(cp.stdout.decode()))
