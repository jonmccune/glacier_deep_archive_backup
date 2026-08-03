#!/usr/bin/env python

import os
import random
import shutil
import subprocess
import sys
import tempfile

import pytest

from impl import do_restore
from impl.tools import BackupException, BackupMode
from impl.upload_sets import package_and_upload_stream
from impl.zfs_stream import (MANIFEST_SUFFIX, StreamManifest, ZfsSendStream,
                             build_manifest_archive, build_stream_archive,
                             estimate_stream_size, extract_manifest_archive,
                             extract_stream_archive, make_archive_prefix,
                             make_receive_cmd, make_send_cmd, parse_send_estimate,
                             split_extra_args)

SCRIPT_PATH = os.path.dirname(os.path.abspath(__file__))
ROOT_PATH = os.path.dirname(SCRIPT_PATH)
STUBS_PATH = os.path.join(SCRIPT_PATH, 'stubs')
PASSPHRASE_FILE = os.path.join(ROOT_PATH, 'config', 'passphrase.txt')

SNAPSHOT = 'tank@snapshot-aws-2022-02-18-195831'
CHUNK_SIZE = 16 * 1024


class TestException(Exception):
    pass


@pytest.fixture(scope='session', autouse=True)
def passphrase():
    '''Provides a passphrase for the gpg calls, without clobbering a real one.'''
    if os.path.exists(PASSPHRASE_FILE):
        yield
        return
    with open(PASSPHRASE_FILE, 'wt') as f:
        print('glacier_deep_archive_backup_test', file=f)
    os.chmod(PASSPHRASE_FILE, 0o600)
    yield
    os.unlink(PASSPHRASE_FILE)


@pytest.fixture(name='work_path')
def work_path_fixture():
    work_path = tempfile.mkdtemp(prefix='gdab_zfs_stream_test_')
    yield work_path
    shutil.rmtree(work_path, ignore_errors=True)


def make_stream_file(work_path, size, seed=42):
    '''Creates the fake `zfs send` stream. The same seed gives the same bytes.'''
    stream_file = os.path.join(work_path, 'pool.bin')
    random.seed(seed)
    data = random.randbytes(size)
    with open(stream_file, 'wb') as f:
        f.write(data)
    return stream_file, data


def cat_cmd(stream_file):
    return ['cat', stream_file]


class FakeUploader:
    '''Stores uploads in a directory instead of talking to S3.

    fail_from_chunk makes every Deep Archive upload from that chunk on fail, like a
    broken internet connection would.
    '''
    def __init__(self, bucket_path, fail_from_chunk=None):
        self.bucket_path = bucket_path
        self.fail_from_chunk = fail_from_chunk
        self.uploads = []
        self.num_deep_archive_uploads = 0
        os.makedirs(bucket_path, exist_ok=True)

    def upload(self, file_, archive_name, deep_archive):
        if deep_archive:
            if (self.fail_from_chunk is not None
                    and self.num_deep_archive_uploads >= self.fail_from_chunk):
                raise subprocess.CalledProcessError(1, 'aws s3 cp')
            self.num_deep_archive_uploads += 1
        shutil.copyfile(file_, os.path.join(self.bucket_path, archive_name))
        self.uploads.append((archive_name, deep_archive))
        return 0.01

    def deep_archive_names(self):
        return [name for name, deep_archive in self.uploads if deep_archive]


def run_backup(work_path, stream_file, manifest, uploader):
    set_path = os.path.join(work_path, 'sets')
    buffer_path = os.path.join(work_path, 'buffer')
    os.makedirs(set_path, exist_ok=True)
    os.makedirs(buffer_path, exist_ok=True)
    manifest_file = os.path.join(set_path, f'tank{MANIFEST_SUFFIX}')
    manifest.save(manifest_file)

    with ZfsSendStream(cat_cmd(stream_file)) as stream:
        num_errors = package_and_upload_stream(stream, manifest, manifest_file,
                                               buffer_path, uploader)

    # Nothing may be left behind, the buffer only has room for a few chunks
    if os.listdir(buffer_path) != []:
        raise TestException(f'Buffer not empty: {os.listdir(buffer_path)}')

    return num_errors, manifest_file


def reassemble(bucket_path, manifest, out_file):
    with open(out_file, 'wb') as f:
        for chunk in manifest.chunks:
            extract_stream_archive(os.path.join(bucket_path, chunk['archive_name']), f,
                                   chunk['sha256'])
    with open(out_file, 'rb') as f:
        return f.read()


#
# Command construction
#


def test_make_send_cmd():
    assert make_send_cmd(SNAPSHOT) == ['sudo', 'zfs', 'send', '-R', SNAPSHOT]
    assert make_send_cmd(SNAPSHOT, recursive=False) == ['sudo', 'zfs', 'send', SNAPSHOT]
    assert make_send_cmd(SNAPSHOT, extra_args=('-w', '-L')) \
        == ['sudo', 'zfs', 'send', '-R', '-w', '-L', SNAPSHOT]
    assert make_send_cmd(SNAPSHOT, estimate=True) \
        == ['sudo', 'zfs', 'send', '-nP', '-R', SNAPSHOT]


def test_make_receive_cmd():
    assert make_receive_cmd('tank_restore') \
        == ['sudo', 'zfs', 'receive', 'tank_restore']
    assert make_receive_cmd('tank_restore', ('-F', '-u')) \
        == ['sudo', 'zfs', 'receive', '-F', '-u', 'tank_restore']


def test_split_extra_args():
    for empty in (None, '', '  '):
        assert not split_extra_args(empty)
    assert split_extra_args('-L -e -c') == ('-L', '-e', '-c')
    assert split_extra_args("-o 'a b'") == ('-o', 'a b')


def test_make_archive_prefix():
    assert make_archive_prefix(SNAPSHOT) == 'tank'
    assert make_archive_prefix('tank/vms@snap') == 'tank_vms'
    assert make_archive_prefix('tank/a b/c@snap') == 'tank_a_b_c'


def test_backup_mode(monkeypatch):
    monkeypatch.delenv('BACKUP_MODE', raising=False)
    assert BackupMode().is_files()
    assert not BackupMode().is_zfs_stream()
    monkeypatch.setenv('BACKUP_MODE', 'zfs_stream')
    assert BackupMode().is_zfs_stream()
    assert not BackupMode().is_files()
    monkeypatch.setenv('BACKUP_MODE', 'ZFS_STREAM')
    assert BackupMode().is_zfs_stream()
    monkeypatch.setenv('BACKUP_MODE', 'nonsense')
    with pytest.raises(BackupException):
        BackupMode()


#
# Stream size estimate
#


def test_parse_send_estimate():
    assert parse_send_estimate('full\ttank@snap\t1234\nsize\t1234\n') == 1234
    # Some ZFS versions print incremental sizes per dataset before the total
    assert parse_send_estimate('incremental\ta\tb\t1\nsize\t99\n') == 99
    assert parse_send_estimate('') is None
    assert parse_send_estimate('cannot send: dataset does not exist\n') is None


def test_estimate_stream_size(work_path, monkeypatch):
    _, data = make_stream_file(work_path, 5000)
    monkeypatch.setenv('GDAB_TEST_DIR', work_path)
    monkeypatch.setenv('PATH', f'{STUBS_PATH}{os.pathsep}{os.environ["PATH"]}')
    assert estimate_stream_size(SNAPSHOT) == len(data)


#
# Manifest
#


def test_manifest_roundtrip(work_path):
    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, 12345, recursive=False,
                              send_extra_args=('-w',))
    manifest.add_chunk(0, manifest.make_archive_name(0), 100, 50, 'aa')
    manifest.add_chunk(1, manifest.make_archive_name(1), 200, 60, 'bb')
    manifest.complete = True

    manifest_file = os.path.join(work_path, 'm.json')
    manifest.save(manifest_file)
    loaded = StreamManifest.load(manifest_file)

    assert loaded.to_dict() == manifest.to_dict()
    assert loaded.uploaded_bytes() == 300
    assert loaded.uploaded_archive_bytes() == 110
    assert loaded.send_extra_args == ['-w']
    assert not loaded.recursive


def test_manifest_archive_names():
    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, 0)
    assert manifest.make_archive_name(0) == 'tank_00000.zfs.zstd.gpg'
    assert manifest.make_archive_name(42) == 'tank_00042.zfs.zstd.gpg'
    # Ascending index must equal ascending name, that is how restore orders the chunks
    names = [manifest.make_archive_name(i) for i in (0, 1, 9, 10, 99, 100, 1000)]
    assert names == sorted(names)


def test_manifest_chunks_must_be_added_in_order():
    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, 0)
    with pytest.raises(BackupException):
        manifest.add_chunk(1, 'x', 1, 1, 'aa')


def test_manifest_version_is_checked():
    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, 0)
    info = manifest.to_dict()
    info['version'] = 99999
    with pytest.raises(BackupException):
        StreamManifest.from_dict(info)


def test_manifest_verify_chunk():
    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, 0)
    manifest.add_chunk(0, 'x', 100, 50, 'aa')
    manifest.verify_chunk(0, 100, 'aa')
    with pytest.raises(BackupException):
        manifest.verify_chunk(0, 100, 'bb')
    with pytest.raises(BackupException):
        manifest.verify_chunk(0, 101, 'aa')


def test_manifest_archive_roundtrip(work_path):
    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, 999)
    manifest.add_chunk(0, manifest.make_archive_name(0), 100, 50, 'aa')
    manifest_file = os.path.join(work_path, 'm.json')
    manifest.save(manifest_file)

    archive_file = os.path.join(work_path, manifest.make_manifest_archive_name())
    build_manifest_archive(manifest_file, archive_file)
    assert extract_manifest_archive(archive_file).to_dict() == manifest.to_dict()


#
# Chunking and the archive pipeline
#


def chunk_stream(work_path, stream_file, size, chunk_size=CHUNK_SIZE):
    '''Chunks the stream the way stream_archiver does, without the upload.'''
    manifest = StreamManifest(SNAPSHOT, chunk_size, size)
    with ZfsSendStream(cat_cmd(stream_file)) as stream:
        index = 0
        while True:
            archive_name = manifest.make_archive_name(index)
            archive_file = os.path.join(work_path, archive_name)
            num_bytes, sha256 = build_stream_archive(stream, archive_file, chunk_size)
            if num_bytes > 0:
                manifest.add_chunk(index, archive_name, num_bytes,
                                   os.path.getsize(archive_file), sha256)
                index += 1
            else:
                os.unlink(archive_file)
            if stream.at_eof:
                break
        stream.finish()
    return manifest


@pytest.mark.parametrize('size,num_expected_chunks', [
    (0, 0),
    (1, 1),
    (CHUNK_SIZE - 1, 1),
    (CHUNK_SIZE, 1),
    (CHUNK_SIZE + 1, 2),
    (3 * CHUNK_SIZE, 3),
    (3 * CHUNK_SIZE + 17, 4),
])
def test_chunking_roundtrip(work_path, size, num_expected_chunks):
    stream_file, data = make_stream_file(work_path, size)
    manifest = chunk_stream(work_path, stream_file, size)

    assert len(manifest.chunks) == num_expected_chunks
    assert manifest.uploaded_bytes() == size
    # Only the last chunk may be short
    for chunk in manifest.chunks[:-1]:
        assert chunk['size_bytes'] == CHUNK_SIZE

    out_file = os.path.join(work_path, 'out.bin')
    assert reassemble(work_path, manifest, out_file) == data


def test_extract_detects_corruption(work_path):
    stream_file, _ = make_stream_file(work_path, 1000)
    manifest = chunk_stream(work_path, stream_file, 1000)
    archive_file = os.path.join(work_path, manifest.chunks[0]['archive_name'])

    with open(os.path.join(work_path, 'out.bin'), 'wb') as f:
        with pytest.raises(BackupException):
            extract_stream_archive(archive_file, f, 'not_the_right_hash')


def test_send_failure_is_reported():
    with ZfsSendStream(['sh', '-c', 'echo hi; exit 3']) as stream:
        stream.copy_chunk(None, CHUNK_SIZE)
        with pytest.raises(BackupException):
            stream.finish()


def test_stop_aborts_copy(work_path):
    stream_file, _ = make_stream_file(work_path, 4 * CHUNK_SIZE)
    with ZfsSendStream(cat_cmd(stream_file)) as stream:
        stream.stop()
        num_bytes, _ = stream.copy_chunk(None, CHUNK_SIZE)
        assert num_bytes == 0
        assert stream.aborted


#
# Backup pipeline
#


def test_backup_uploads_all_chunks(work_path):
    size = 3 * CHUNK_SIZE + 100
    stream_file, data = make_stream_file(work_path, size)
    bucket_path = os.path.join(work_path, 'bucket')
    uploader = FakeUploader(bucket_path)
    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, size)

    num_errors, _ = run_backup(work_path, stream_file, manifest, uploader)

    assert num_errors == 0
    assert manifest.complete
    assert manifest.uploaded_bytes() == size
    expected_names = [f'tank_{i:05d}.zfs.zstd.gpg' for i in range(4)]
    assert uploader.deep_archive_names() == expected_names

    # The manifest in the bucket must describe the backup that is in the bucket
    uploaded = extract_manifest_archive(
        os.path.join(bucket_path, manifest.make_manifest_archive_name()))
    assert uploaded.complete
    assert uploaded.to_dict() == manifest.to_dict()

    assert reassemble(bucket_path, uploaded, os.path.join(work_path, 'out.bin')) == data


def test_backup_of_empty_stream(work_path):
    stream_file, _ = make_stream_file(work_path, 0)
    uploader = FakeUploader(os.path.join(work_path, 'bucket'))
    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, 0)

    num_errors, _ = run_backup(work_path, stream_file, manifest, uploader)

    assert num_errors == 0
    assert manifest.chunks == []
    assert uploader.deep_archive_names() == []


def test_backup_stops_on_upload_error(work_path):
    size = 4 * CHUNK_SIZE
    stream_file, _ = make_stream_file(work_path, size)
    uploader = FakeUploader(os.path.join(work_path, 'bucket'), fail_from_chunk=2)
    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, size)

    num_errors, _ = run_backup(work_path, stream_file, manifest, uploader)

    assert num_errors > 0
    assert not manifest.complete
    # Chunks form one stream, so no chunk past the failed one may be uploaded
    assert len(manifest.chunks) == 2
    expected_names = ['tank_00000.zfs.zstd.gpg', 'tank_00001.zfs.zstd.gpg']
    assert uploader.deep_archive_names() == expected_names


def test_backup_resumes_where_it_stopped(work_path):
    size = 4 * CHUNK_SIZE + 5
    stream_file, data = make_stream_file(work_path, size)
    bucket_path = os.path.join(work_path, 'bucket')

    failing_uploader = FakeUploader(bucket_path, fail_from_chunk=2)
    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, size)
    num_errors, manifest_file = run_backup(work_path, stream_file, manifest,
                                           failing_uploader)
    assert num_errors > 0

    resumed = StreamManifest.load(manifest_file)
    assert len(resumed.chunks) == 2
    uploader = FakeUploader(bucket_path)
    with ZfsSendStream(cat_cmd(stream_file)) as stream:
        num_errors = package_and_upload_stream(stream, resumed, manifest_file,
                                               os.path.join(work_path, 'buffer'),
                                               uploader)

    assert num_errors == 0
    assert resumed.complete
    assert resumed.uploaded_bytes() == size
    # The already uploaded chunks are re-read from the stream, but not re-uploaded
    expected_names = ['tank_00002.zfs.zstd.gpg', 'tank_00003.zfs.zstd.gpg',
                      'tank_00004.zfs.zstd.gpg']
    assert uploader.deep_archive_names() == expected_names
    assert reassemble(bucket_path, resumed, os.path.join(work_path, 'out.bin')) == data


def test_backup_detects_non_reproducible_stream(work_path):
    size = 4 * CHUNK_SIZE
    stream_file, _ = make_stream_file(work_path, size)
    bucket_path = os.path.join(work_path, 'bucket')

    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, size)
    num_errors, manifest_file = run_backup(work_path, stream_file, manifest,
                                           FakeUploader(bucket_path, fail_from_chunk=2))
    assert num_errors > 0

    # A second `zfs send` of the same snapshot producing different bytes must not
    # silently result in a backup that cannot be received
    make_stream_file(work_path, size, seed=4711)
    resumed = StreamManifest.load(manifest_file)
    uploader = FakeUploader(bucket_path)
    with ZfsSendStream(cat_cmd(stream_file)) as stream:
        num_errors = package_and_upload_stream(stream, resumed, manifest_file,
                                               os.path.join(work_path, 'buffer'),
                                               uploader)

    assert num_errors > 0
    assert uploader.deep_archive_names() == []
    assert len(resumed.chunks) == 2


def test_backup_send_failure_is_reported(work_path):
    os.makedirs(os.path.join(work_path, 'sets'), exist_ok=True)
    os.makedirs(os.path.join(work_path, 'buffer'), exist_ok=True)
    manifest = StreamManifest(SNAPSHOT, CHUNK_SIZE, 100)
    manifest_file = os.path.join(work_path, 'sets', 'm.json')
    manifest.save(manifest_file)
    uploader = FakeUploader(os.path.join(work_path, 'bucket'))

    with ZfsSendStream(['sh', '-c', 'echo partial; exit 3']) as stream:
        num_errors = package_and_upload_stream(stream, manifest, manifest_file,
                                               os.path.join(work_path, 'buffer'),
                                               uploader)

    assert num_errors > 0
    assert not manifest.complete


#
# Restore helpers shared by both backup modes
#


@pytest.fixture(name='bucket')
def bucket_fixture(work_path, monkeypatch):
    bucket_path = os.path.join(work_path, 'bucket')
    os.makedirs(bucket_path, exist_ok=True)
    monkeypatch.setenv('GDAB_BUCKET_DIR', bucket_path)
    monkeypatch.setenv('PATH', f'{STUBS_PATH}{os.pathsep}{os.environ["PATH"]}')
    return bucket_path


def put_object(bucket_path, key, content, deep_archive=False):
    path = os.path.join(bucket_path, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(content)
    if deep_archive:
        with open(os.path.join(bucket_path, '.deep_archive_keys'), 'at') as f:
            print(key, file=f)


def test_get_files_lists_only_deep_archive(bucket, work_path):
    del work_path
    put_object(bucket, 'dir/ts/a.zfs.zstd.gpg', b'12345', deep_archive=True)
    put_object(bucket, 'dir/ts/tank.manifest.json.zstd.gpg', b'x')
    put_object(bucket, 'dir/other/b.zfs.zstd.gpg', b'y', deep_archive=True)

    files = do_restore.get_files('test_bucket', 'dir', 'ts')

    assert files == [['dir/ts/a.zfs.zstd.gpg', 5]]


def test_get_manifest_key(bucket, work_path):
    del work_path
    put_object(bucket, 'dir/ts/a.zfs.zstd.gpg', b'x', deep_archive=True)
    put_object(bucket, 'dir/ts/tank.manifest.json.zstd.gpg', b'y')

    assert do_restore.get_manifest_key('test_bucket', 'dir',
                                       'ts') == 'dir/ts/tank.manifest.json.zstd.gpg'


def test_get_manifest_key_throws_when_missing(bucket, work_path):
    del work_path
    put_object(bucket, 'dir/ts/a.zfs.zstd.gpg', b'x', deep_archive=True)

    with pytest.raises(BackupException):
        do_restore.get_manifest_key('test_bucket', 'dir', 'ts')


def test_is_restored(bucket, work_path):
    del work_path
    put_object(bucket, 'dir/ts/a.zfs.zstd.gpg', b'x', deep_archive=True)

    assert do_restore.is_restored('test_bucket', 'dir/ts/a.zfs.zstd.gpg')


def test_download(bucket, work_path):
    put_object(bucket, 'dir/ts/a.zfs.zstd.gpg', b'payload', deep_archive=True)
    dest = os.path.join(work_path, 'download')
    os.makedirs(dest)

    key = 'dir/ts/a.zfs.zstd.gpg'
    local_path = do_restore.download('test_bucket', key, dest, '1/1')

    assert local_path == os.path.join(dest, 'a.zfs.zstd.gpg')
    with open(local_path, 'rb') as f:
        assert f.read() == b'payload'


def test_download_failure_throws(bucket, work_path):
    del bucket
    dest = os.path.join(work_path, 'download')
    os.makedirs(dest)

    with pytest.raises(BackupException):
        do_restore.download('test_bucket', 'dir/ts/missing', dest, '1/1')


#
# End to end through the real scripts, with zfs and the AWS CLI stubbed out
#


def make_test_repo(work_path):
    '''Copies the repo so the scripts can write to state/, logs/ and config/.'''
    repo_path = os.path.join(work_path, 'repo')
    shutil.copytree(ROOT_PATH, repo_path,
                    ignore=shutil.ignore_patterns('.git', 'work', '__pycache__'))
    # Start from a clean state, a real backup may be pending in the repo
    for dir_ in ('state', 'logs'):
        shutil.rmtree(os.path.join(repo_path, dir_), ignore_errors=True)
    os.makedirs(os.path.join(repo_path, 'state', 'sets'))
    os.makedirs(os.path.join(repo_path, 'logs'))
    return repo_path


def make_python_shim_path(work_path):
    '''The scripts use `#!/usr/bin/env python`, not all systems provide that name.'''
    if shutil.which('python') is not None:
        return []
    shim_path = os.path.join(work_path, 'shims')
    os.makedirs(shim_path, exist_ok=True)
    python_shim = os.path.join(shim_path, 'python')
    with open(python_shim, 'wt') as f:
        print('#!/bin/sh', file=f)
        print(f'exec {sys.executable} "$@"', file=f)
    os.chmod(python_shim, 0o755)
    return [shim_path]


def make_env(work_path, repo_path):
    env = dict(os.environ)
    path_entries = [STUBS_PATH] + make_python_shim_path(work_path)
    env['PATH'] = os.pathsep.join(path_entries + [env['PATH']])
    env['PYTHONPATH'] = repo_path
    env['PYTHONUNBUFFERED'] = '1'
    env['GDAB_TEST_DIR'] = work_path
    env['GDAB_BUCKET_DIR'] = os.path.join(work_path, 'bucket')
    os.makedirs(env['GDAB_BUCKET_DIR'], exist_ok=True)
    return env


def write_backup_config(repo_path, work_path, upload_limit_mb, extra=''):
    config_path = os.path.join(repo_path, 'config', 'backup_test.sh')
    with open(config_path, 'wt') as f:
        f.write(f'''set -euo pipefail
ZFS_POOL=tank
BACKUP_MODE=zfs_stream
BACKUP_PATHS=()
S3_BUCKET=test_bucket
BUCKET_DIR=testdir
UPLOAD_LIMIT_MB={upload_limit_mb}
BUFFER_PATH_BASE='{work_path}'
{extra}
''')
    return config_path


def test_end_to_end_backup_and_restore(work_path):
    '''Runs do_backup_to_aws.sh and restore with stubbed zfs/aws.

    This covers the whole chain the way a user runs it: the shell wrapper, chunking,
    compression, encryption, upload, the bucket layout, and restoring the stream back
    into `zfs receive`.
    '''
    # 2.5 MiB of data with a 1 MiB upload limit gives three chunks
    _, data = make_stream_file(work_path, 2 * 1024 * 1024 + 512 * 1024)
    repo_path = make_test_repo(work_path)
    env = make_env(work_path, repo_path)
    config_path = write_backup_config(repo_path, work_path, 1)

    cp = subprocess.run(('impl/do_backup_to_aws.sh', 'scratch', config_path),
                        cwd=repo_path, env=env, check=False, capture_output=True,
                        text=True)
    if cp.returncode != 0:
        raise TestException(f'Backup failed:\n{cp.stdout}\n{cp.stderr}')

    bucket_path = env['GDAB_BUCKET_DIR']
    keys = sorted(os.path.relpath(os.path.join(root, file_), bucket_path)
                  for root, _, files in os.walk(bucket_path) for file_ in files)
    chunk_keys = [key for key in keys if key.endswith('.zfs.zstd.gpg')
                  and '.manifest' not in key]
    if len(chunk_keys) != 3:
        raise TestException(f'Expected 3 chunks, got {chunk_keys} (all keys: {keys})')

    # The snapshot must be taken and sent recursively
    with open(os.path.join(work_path, 'send_invocations'), 'rt') as f:
        assert '-R' in f.read()

    # The uploaded restore config must be the one for this mode
    restore_configs = [key for key in keys if key.endswith('.sh')]
    assert len(restore_configs) == 1, keys
    with open(os.path.join(bucket_path, restore_configs[0]), 'rt') as f:
        assert 'BACKUP_MODE=zfs_stream' in f.read()

    timestamp = os.path.basename(os.path.dirname(chunk_keys[0]))
    restore_config_path = os.path.join(repo_path, 'config', 'restore_test.sh')
    with open(restore_config_path, 'wt') as f:
        f.write(f'''set -euo pipefail
S3_BUCKET=test_bucket
BUCKET_DIR=testdir
TIMESTAMP={timestamp}
RESTORE_TIER=Bulk
BACKUP_MODE=zfs_stream
ZFS_RECV_TARGET=tank_restore
ZFS_RECV_EXTRA_ARGS='-F -u'
BUFFER_PATH_BASE='{work_path}'
''')

    cp = subprocess.run(('./restore', restore_config_path), cwd=repo_path, env=env,
                        check=False, capture_output=True, text=True)
    if cp.returncode != 0:
        raise TestException(f'Restore failed:\n{cp.stdout}\n{cp.stderr}')

    with open(os.path.join(work_path, 'received.bin'), 'rb') as f:
        received = f.read()
    if received != data:
        raise TestException(f'Received {len(received)} bytes, sent {len(data)} bytes,'
                            ' content differs')

    with open(os.path.join(work_path, 'receive_invocations'), 'rt') as f:
        receive_args = f.read()
    assert '-F -u tank_restore' in receive_args, receive_args


def test_end_to_end_resume(work_path):
    '''An interrupted backup is completed by backup_resume, without re-uploading.'''
    _, data = make_stream_file(work_path, 2 * 1024 * 1024 + 512 * 1024)
    repo_path = make_test_repo(work_path)
    env = make_env(work_path, repo_path)
    config_path = write_backup_config(repo_path, work_path, 1)

    # Make the third Deep Archive upload fail, so the backup stops after two chunks
    fail_marker = os.path.join(work_path, 'fail_after')
    with open(fail_marker, 'wt') as f:
        print('2', file=f)
    env['GDAB_FAIL_DEEP_ARCHIVE_AFTER'] = fail_marker

    cp = subprocess.run(('impl/do_backup_to_aws.sh', 'scratch', config_path),
                        cwd=repo_path, env=env, check=False, capture_output=True,
                        text=True)
    assert cp.returncode != 0, cp.stdout
    assert os.path.exists(os.path.join(repo_path, 'state', 'resume_info'))

    del env['GDAB_FAIL_DEEP_ARCHIVE_AFTER']
    cp = subprocess.run(('impl/do_backup_to_aws.sh', 'resume'), cwd=repo_path, env=env,
                        check=False, capture_output=True, text=True)
    if cp.returncode != 0:
        raise TestException(f'Resume failed:\n{cp.stdout}\n{cp.stderr}')
    assert 'uploaded by a previous run' in cp.stdout

    bucket_path = env['GDAB_BUCKET_DIR']
    keys = sorted(os.path.relpath(os.path.join(root, file_), bucket_path)
                  for root, _, files in os.walk(bucket_path) for file_ in files)
    chunk_keys = [key for key in keys if key.endswith('.zfs.zstd.gpg')
                  and '.manifest' not in key]
    assert len(chunk_keys) == 3, keys

    manifest_key = [key for key in keys if key.endswith('.manifest.json.zstd.gpg')][0]
    manifest = extract_manifest_archive(os.path.join(bucket_path, manifest_key))
    assert manifest.complete
    assert manifest.uploaded_bytes() == len(data)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
