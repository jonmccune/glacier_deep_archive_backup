# Introduction

**Note: If you are relying on this for your last-resort backup, please read everything
below!**

`glacier_deep_archive_backup` (GDAB) is a backup solution that uses
[S3 Glacier Deep Archive](https://aws.amazon.com/blogs/aws/new-amazon-s3-storage-class-glacier-deep-archive/)
for storage. This is the most cost effective cloud backup storage I'm aware of. The use
case is for full, off-site, encrypted backups that are only retrieved after a catastrophe
(i.e. your house burns down, RAID *and* local backups are gone). This is reflected by the
[cost structure](https://aws.amazon.com/s3/pricing/) and properties of Deep Archive:

- Upload is free
- Storage is $0.00099 per GiB/month ($1.01376/TiB). That makes it very cheap to store your
  data.
- Restore and download is quite costly:
    - Restore from S3 tape to S3 blob:
        - $0.0025/GiB ($2.56/TiB) for Bulk within 48 hours
        - $0.02/GiB ($20.48/TiB) for Standard within 12 hours
    - Download: The first 100 GiB/month are free, then 10 TiB/Month for $0.09 per GiB
      ($92.16/TiB) and discounts for more.
    - **Update:** With the [European Data Act coming into force](https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=OJ:L_202302854&qid=1709707090487#d1e3746-1-1),
      the cost structure likely changes significantly: Egress fees are no longer permitted,
      and [AWS has admitted as much](https://aws.amazon.com/blogs/aws/free-data-transfer-out-to-internet-when-moving-out-of-aws/),
      although they are tying this to "moving out of AWS" (without checking). So while you
      shouldn't rely on it, it's probably safe to assume that you won't need to pay the
      download fees above, which are 90% of the recovery costs, so for bulk you only pay
      $2.56/TiB. Likely only applies to EU.

- The assumption is that you will hopefully never need to restore ;) (and for testing, you
  can use the 100 GiB free download where you only pay for restore)
- Note: The minimum storage charge is for 180 days. So even if you delete your data
  earlier, you still pay. So this backup solution is not meant for fast-changing data, but
  it allows you to retain quite a few full backups that you can restore directly without
  applying several incremental backups on top.
- Data is stored across 3 or more AWS Availability Zones, providing
  [99.999999999% data durability](https://aws.amazon.com/s3/storage-classes/#____)
- All quoted prices are for US East, some regions are more expensive

The script relies on the ZFS file system's snapshot capabilities to provide a consistent
state of the file system during upload (which can take days). So this will only work for
data on ZFS pools, although it could be adapted to work for other snapshotting file
systems or by losening the consistency requirements.

There are two backup modes, selected with `BACKUP_MODE` in your backup config:

- `files` (the default): The snapshot is mounted and the files you list are packed into
  `tar` archives. This is the mode all of the documentation below refers to unless
  stated otherwise, and the only one that supports sealing and incremental backups.
- `zfs_stream`: A `zfs send` stream of a whole pool or dataset is backed up block level,
  and restored with `zfs receive`. This covers everything ZFS knows about, including
  zvols, dataset properties and encryption, none of which a file level backup can
  capture. See [Whole-Pool Backups With `zfs send`](#whole-pool-backups-with-zfs-send).

# Installation

## Prerequisites

- Unix-ish system
- ZFS pool(s)
- Recent python
- `zstd`
- `gpg`
- `pip install binpacking`
- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- An AWS account and an S3 bucket. Follow [these instructions](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-prereqs.html)
  to set up an account and put the credentials in `~/.aws/credentials`. The file should
  look similar to this:

    ```
    [default]
    aws_access_key_id = XESRNTIERINVALIDNSTIENRSITE
    aws_secret_access_key = inewtn8un3invalid289tneirnst
    region = us-east-2
    ```

    `region` is where your S3 bucket is located. Since this is an off-site backup, it
    should *not* be the region most closest to you. Best to choose a different continent ;)

- `pip install pytest` if you want to run the tests

## Installation

Just clone, or download and unzip.

```
git clone git@github.com:mrichtarsky/glacier_deep_archive_backup.git
```

```
wget https://github.com/mrichtarsky/glacier_deep_archive_backup/archive/refs/heads/main.zip
unzip main.zip
rm main.zip
```

# Usage

## Backup

- `cp config/backup_example.sh config/backup.sh` and edit `backup.sh` to reflect your
  setup. In `BACKUP_PATHS`, list all the files and directories that will be backed up
  recursively. Make sure to use double quotes to escape names that `"have spaces"` etc.
  You can use wildcards, see the notes in the file for details.
- Edit `config/passphrase.txt` and put a strong passphrase there. Your data will be
  encrypted locally using this password before upload. **Make sure this file is only
  readable/writable by you!**

- Run `./backup_scratch config/backup.sh` to start the backup. Logs are in
  `logs/backup_scratch.log`.
- If the backup fails for some reason (e.g. internet down), run `./backup_resume` to
  continue (logs are in `logs/backup_resume.log`)
- If you have several pools, simply create a separate `backup_poolname.sh` for each and
  do a corresponding scratch backup (e.g. `./backup_scratch backup_tank.sh`).
  **However, you cannot run backups in parallel since the `state` directory is meant to
  only handle one backup.**

An unprivileged user can run the scripts. Make sure this user has all the necessary
permissions for reading the data to be backed up. Only the following operations are
executed as `root`, for which the user must have `sudo` privileges:
- Creating, mounting, unmounting and destroying the ZFS snapshot
- Creating the path for the snapshot mount
- `zfs send`/`zfs receive` when using `BACKUP_MODE=zfs_stream`
- For `chattr` when using sealing
- For the fuzz test, creating a `tmpfs`

Backups are saved in your S3 bucket in a timestamped directory (e.g. `2022-02-18-195831`).
This directory contains pairs of files:

```
tank_pics_000.list_contents.tar.zstd.gpg

# The list of dirs/files stored in the archive,
# as passed to tar. Will not recursively list files of
# a directory if that directory was passed to tar.

tank_pics_000.tar.zstd.gpg
# The tar archive, zstd compressed, aes256 encrypted
```

(With `BACKUP_MODE=zfs_stream` the directory contains numbered stream chunks and a
manifest instead, see [below](#whole-pool-backups-with-zfs-send).)

You should store everything you need to restore the backup in a safe location:

- Your passphrase
- Credentials to access AWS

Consider you want to be able to retrieve these backups when your house has burnt down.

You should also test a full backup/restore cycle once. It can involve a small subset of
data, e.g. 10 GiB, which will make the test cheap.

### Sealing

When your backup has lots of data which does not change, you can use _sealing_ to
reduce upload times. Other backup solutions use incremental backups, reducing the
amount of data uploaded after the first backup. However, it is discouraged to create
long backup chains since the risk of something breaking increases, so even with
incremental backups you will have to backup _all_ of your data from time to time. Sealing
changes this. Let's assume you have a directory containing the pictures you made each
year. At the end of the year, the directory does not change anymore:

```
pictures/2021 <-- no changes
         2022 <-- no changes
         2023 <-- no changes
         2024 <-- still changes
```

With sealing, you create a backup config that specifies all past years. In the config
you also set `SEAL_ACTION=seal_after_backup` and then do a scratch backup. You will
get a backup (or more fitting: archive) of that data, and you'll never have to back it
up again. To ensure the data does not get modified afterwards, all specified
directories will be marked as immutable (recursively), preventing changes even by `root`.

However, what happens with the directory `2024` above, which still receives changes
that you want to backup regularly? With sealing, the whole `pictures` directory can be
specified in the regular backup, but all sealed directories _will be skipped_. For this,
you specify `SEAL_ACTION=skip_sealed` in your backup config.

For a full restore, you will first need to restore the sealed backup, then your regular
one.

Here are two example backup configs for illustration:


```
config/backup_pictures_sealed:

BACKUP_PATHS=(
    pictures/2021
    pictures/2022
    pictures/2023
)

SEAL_ACTION=seal_after_backup
```

```
config/backup_pictures:

BACKUP_PATHS=(
    pictures
)

SEAL_ACTION=skip_sealed
```

- Sealing is optional, you do not have to use it (just set to `disable` or do not
  specify `SEAL_ACTION` at all)
- Sealed directories are marked with a symlink `.GDAB_SEALED` at the root which then
  applies recursively. The symlink points to the backup config.
- Of course, you do not `expire` sealed backups, they remain forever, since the data
  will not be part of another backup.
- For adding sealed directories (e.g. when the year 2024 is over), you can
  either:
  - Extend the original backup config and redo the whole backup, deleting the old one
  - Create a new backup config just for the new directories and backup that in addition
- If you ever need to unseal a directory, make sure you unseal the whole directory tree:
  - Go to the corresponding backup path that was specified in your config (e.g.
  `pictures/2023`), which has the  `.GDAB_SEALED` symlink.
  - Run `sudo chattr -R -i .`
  - `rm .GDAB_SEALED`
  - You can use the script `unseal` as follows to unseal all directories of a backup
    config:
    ```
    ./unseal config/backup_pictures_sealed
    ```
  - Make sure that data is covered by a regular backup.

### Whole-Pool Backups With `zfs send`

The default `files` mode mounts the snapshot and packs files with `tar`. That covers
everything you can see in a file system, but not everything ZFS stores:

- **zvols**, i.e. raw block devices used as VM disks. There is no directory tree to
  walk and no file to `tar` up.
- Dataset **properties** (`recordsize`, `compression`, quotas, mountpoints, ...),
  encryption roots and keys, and the dataset layout itself.
- Snapshots other than the one being backed up.

With `BACKUP_MODE=zfs_stream` the data source is a `zfs send` stream instead. Everything
after that is the same machinery the file mode uses: the stream is split into chunks of
`UPLOAD_LIMIT_MB`, each chunk is `zstd` compressed and `gpg` AES256 encrypted, uploaded
to Deep Archive, and a failed backup is continued with `./backup_resume`.

Setting it up:

```
config/backup_tank.sh:

ZFS_POOL=tank
BACKUP_MODE=zfs_stream

# Optional, defaults to ZFS_POOL. Set to back up a subtree, e.g. tank/vms
ZFS_SEND_DATASET=tank

# Optional, defaults to 1: send the dataset and all its descendants (`zfs send -R`)
ZFS_SEND_RECURSIVE=1

# Optional, extra arguments for `zfs send`, see below
ZFS_SEND_EXTRA_ARGS=''
```

`BACKUP_PATHS`, `SNAPSHOT_PATH` and `SEAL_ACTION` are not used in this mode. Then run
`./backup_scratch config/backup_tank.sh` as usual.

Your S3 bucket will contain the numbered chunks of the stream plus a manifest:

```
tank_00000.zfs.zstd.gpg
tank_00001.zfs.zstd.gpg

# Chunks of the `zfs send` stream, zstd compressed, aes256 encrypted.
# Concatenating them in ascending order reproduces the stream.

tank.manifest.json.zstd.gpg

# Encrypted list of the chunks with their sizes and sha256 sums, and the
# snapshot they came from. Restore uses it to know how many chunks to
# expect, in which order, and to verify each one.
```

#### Notes

- **The stream is never written to local disk in full.** Chunks are produced as the
  stream is read, so the pool can be far larger than the free space on the machine
  running the backup. As for the file mode, `BUFFER_PATH_BASE` needs room for about
  two chunks.
- **A `zfs send` stream is all-or-nothing.** You cannot restore a single file from it
  without receiving the whole thing first, and a lost chunk in the middle makes
  everything after it useless. This is the trade-off for capturing zvols and ZFS
  metadata. If you mainly care about being able to get individual files back cheaply,
  use the `files` mode, possibly in addition.
- **Resuming re-reads the pool.** `zfs send` cannot be restarted in the middle of a
  stream, so `./backup_resume` runs it again from the start and discards the part that
  was already uploaded, rather than re-uploading it. Reading is much cheaper than
  uploading, but it is not free. Each skipped chunk is checked against the sha256 in
  the manifest, so if your ZFS version does not produce a byte identical stream on the
  second run, you get an error instead of a backup that cannot be received.
- **Do not use sealing or duplicity with this mode.** Sealing operates on directories,
  and duplicity on files. Combining them is rejected.
- Useful `ZFS_SEND_EXTRA_ARGS`:
    - `-w` — raw send. **Required for encrypted datasets** if you want them to stay
      encrypted with their own key, and the only way to send them at all when the key
      is not loaded.
    - `-L` — send large blocks, needed if any dataset has `recordsize` > 128k.
    - `-c` — send already compressed blocks as they are. Saves CPU, but leaves `zstd`
      less to work with.
    - `-e` — more compact stream for embedded blocks.
- The size shown in the progress display comes from `zfs send -nP`, which is an
  estimate. The percentages can therefore go slightly over or stop slightly under 100%.
- All `zfs` commands are run through `sudo`, like the snapshot handling of the file
  mode.

#### Restoring a `zfs send` Backup

The generated restore config in your bucket already has `BACKUP_MODE=zfs_stream` set.
Fill in the target and run `./restore` as usual:

```
config/restore.sh:

BACKUP_MODE=zfs_stream

# The dataset to receive into. Its contents will be DESTROYED, see below
ZFS_RECV_TARGET=tank_restore

# -F is required for the recursive streams this tool sends, -u avoids mounting
# the received file systems during the restore
ZFS_RECV_EXTRA_ARGS='-F -u'
```

- **`ZFS_RECV_TARGET` must not be a dataset holding data you care about.** `zfs receive
  -F` rolls the target back and destroys anything in it that is not in the stream.
  Restore into a fresh pool or dataset.
- The chunks are downloaded and received strictly in order, since together they are a
  single stream. Restores for all chunks are requested up front and run in parallel on
  the AWS side, so this does not make the 12/48 hour wait any longer, and the buffer
  only ever holds one chunk.
- Each chunk is verified against its sha256 from the manifest before it reaches
  `zfs receive`.
- If a backup was interrupted and never resumed, restore warns that the manifest is
  incomplete and receives what is there. `zfs receive` will reject the truncated
  stream, which is the correct outcome.

To restore manually, e.g. only to check that the data is good, download the chunks and
pipe them through `extract_stream_archive` in ascending order:

```
for f in tank_0*.zfs.zstd.gpg; do ./extract_stream_archive "$f"; done \
    | sudo zfs receive -F -u tank_restore
```

### Incremental Backups

As mentioned, you always pay for 180 days for each byte you allocate. So the most
cost-efficient strategy is to do a full backup every 180 days  (and when using sealing,
only backing up the data that is not sealed). Of course, that leaves a half-year gap
where data can go missing. On the other hand, a full backup each month will cause higher
costs. Deleting the unneeded older full backups does not help, you will pay for 180
days. Here, performing incremental backups in between the full backups make sense:

- The length of the incremental chain will be reasonable, e.g. when backing up each
  month there will only be six backups involved. So the risk that something along the
  chain breaks is still small.
- More importantly, due to sealing, you already have reduced the amount you have to
  transfer for each full backup considerably.

Therefore, GDAB also offers the option to do incremental backups using
[`Duplicity`](https://duplicity.us/), which is very efficient. See the website for
install instructions. In GDAB, use `backup_duplicity_full` and
`backup_duplicity_incremental` to perform such backups. Why not just use Duplicity? If
you do not want to use sealing, you should indeed use it directly. But with sealing, you
can considerably reduce the time your full backups take.

With sealing and incremental backups, your backup strategy looks as follows:

- Create a backup config `config/all` with `SEAL_ACTION=skip_sealed` that covers all the
  data you want to back up.

- At the start of January and July:
  1. Check which directories will not change anymore/can be archived (e.g. `pics/2024`).
     Create a new backup config with `SEAL_ACTION=seal_after_backup` for it and run
     `backup_scratch`.
  2. For the rest of your data, create an initial full backup using
     `backup_duplicity_full config/all`. Your backup config has `SEAL_ACTION=skip_sealed
     and thus only backs up all non-archived files.

- At the start of each other month:
  1. Run `backup_duplicity_incremental config/all`. This will create an incremental
     backup which will be very quick.

## Restore

- `cp config/restore_example.sh config/restore.sh` and edit `restore.sh` to reflect your
  setup
- Run `./restore config/restore.sh`. This attempts to restore all files to the location
  specified.
- Note that it is not possible to directly download files from Deep Archive: First you
  need to schedule an archive for restore, which will basically retrieve it from tape
  and put it in S3 blob storage on AWS side. Then it can be downloaded. The script
  automates all this, including:
    - Waiting for the file to become available
    - Downloading
    - Decryption
    - Extraction
- Restore will incur costs for restore and transfer. See above for a detailed breakdown.
- Set `BACKUP_MODE` in the restore config to what the backup was made with. For
  `zfs_stream` backups the archives are received with `zfs receive` instead of being
  extracted, see
  [Restoring a `zfs send` Backup](#restoring-a-zfs-send-backup).

Should you wish to only restore some files to save time or money you can follow these
manual steps:

- In the S3 web interface select the file and initiate restore. You can choose between
  Standard and Bulk retrieval (Bulk $2.56/TiB, Standard $20.48/TiB).
- Once the file is available, download it
- Use `./extract_archive ARCHIVE DEST_PATH` to decrypt and extract it (e.g.
  `./extract_archive tank_pics_000.tar.zstd.gpg /tank_restore`). For a
  `BACKUP_MODE=zfs_stream` backup, use `./extract_stream_archive ARCHIVE` instead, see
  [above](#restoring-a-zfs-send-backup).

### Restoring Incremental Backups

When you have made a backup using the Duplicity wrappers `backup_duplicity_*` , you can restore it
directly with `duplicty s3://your_s3_bucket/bucket_dir`. You can also use any
of Duplicity's options for e.g. restoring to a certain time or only restoring select
parts.

# Misc

- Data is encrypted using [`gpg`](https://www.gnupg.org/) (`AES256` cipher)

- In addition to the costs above, there are these small additional charges:
    - $0.10/1000 requests for data retrieval
    - $0.05/1000 PUT, COPY, POST, LIST requests
    - $0.0004/1000 GET, SELECT, and all other requests
    - 8 KB overhead/file billed at S3 standard storage rates
    - 32 KB overhead/file billed at Deep Archive rates

    The script creates archives, so the number of files stored is very small. For an
    upload limit of 50 GiB, you would get 40 files/TiB. So these costs are negligible.
    Most other backup solutions operate 1:1 at a file level, causing tremendous costs
    (see alternatives below).

    You can check the full details on the [pricing page](https://aws.amazon.com/s3/pricing/).

- Symlinks are *not* followed. Therefore, links pointing to files not covered by the
  paths backed up will not be considered! (Not applicable to `BACKUP_MODE=zfs_stream`,
  which does not look at files at all.)

- No file splitting: The largest file must fit into `UPLOAD_LIMIT_MB`. This is checked
  at the beginning. (Not applicable to `BACKUP_MODE=zfs_stream`, which splits the
  stream at exactly `UPLOAD_LIMIT_MB`.)

- There is a progress display which works as follows:

    ```
    Elapsed: 10h 38m, Archived: 0.25/2.71 TiB (9.2%, 17.49 MiB/s),
    Uploaded: 0.12/2.71 TiB (4.6%, 5.12 MiB/s), Ratio: 1.0x, ETA: 7d 8h 39m - 7d 20h 2m
    ```

    - Uploaded bytes are gross (before compression)
    - Upload speed is for net uploaded bytes (after compression)
    - Ratio shows the achieved compression
    - For ETA, a range is estimated:
      - The lower bound assumes that the achieved compression rate remains the same for
        the remaining files
      - The upper bound assumes that compression is 1x for the remaining files
      - It's possible that the lower bound is still too high when the remaining files
        compress better

- Backup files are not deleted in your S3 bucket, you need to take care of this yourself.
  There is an [`expire`](https://github.com/mrichtarsky/glacier_deep_archive_backup/blob/main/expire)
  script you can use for that.

- Since typically archive building is faster than upload, archive building will create
  the next archive in the background, so that the upload can run continuously after the
  ramp up. This leads to about 25% reduction of elapsed time on my server/connection.

- Discussed on [Hacker News](https://news.ycombinator.com/item?id=32864052)

# Alternatives

There are other backup solutions that can target Deep Glacier:

- [`duplicity`](https://duplicity.gitlab.io/) - This is a great tool if you want to have
  incremental backups. It will use rsync-style signatures to only transmit differences
  of files, and therefore can perform very efficient incremental backups. At the same
  time, it creates `tar` archives, so does not sacrifice efficiency on Deep Archive. I
  would probably not have written the tool here had I been aware of this earlier ;) The
  only downside is that restore does not recover files from Deep Archive automatically,
  you have to do that manually, and wait for availability, before any operations like
  `verify` or `restore` can work. If you only care about full backups, `duplicity` does
  not buy you much, in fact it will cause some overhead due to the signatures stored
  both locally and on the backup, which are only needed for incremental backups. Also,
  since you have to do a full backup from time to time, you will have to upload all
  your data, since sealing is not available.

- [`rclone`](https://rclone.org/)
- [`aws sync`](https://awscli.amazonaws.com/v2/documentation/api/latest/reference/s3/sync.html)

   Both tools store files 1:1 in Deep Archive. Due to the cost structure this is
   prohibitively expensive, and the reason why GDAB creates
   archives. `aws sync` only supports server-side encryption, instead of client-side as
   done by this script. It cannot restore files automatically out of Deep Archive, while
   for `rclone` it's a manual step. This script does it automatically and also waits for
   the files to become available, to get the restore done as fast as possible.
- [Arq](https://www.arqbackup.com/) - Only supports Windows/macOS. Apart from that it
  looks pretty good, the amount of files created is already 40 for ~700 MiB of data, but
  it probably scales much better than a 1:1 copy. Also able to restore single files, and
  automatically requests restores and waits for them.

# ToDo

- Testing
  - Tests need config/passphrase, generate automatically
  - Add automated E2E test
      - Including sealing, duplicity
      - Also for insufficient disk space
      - Upload to standard storage to skip waiting times
  - Factor out creation of files to be backed up and use in unit and E2E tests
  - Add symlinks to test
  - Add test for case where archiver thread runs into error


- Wrap tar, count lines and show progress bar
  - Show a file every second
  - Delete that line
  - [1/200] /path/to/file size

- Increase zstd compression/make configurable
  - Compression rarely uses CPU, gpg takes up the majority. So raising it would be
    possible. On the other hand, when backing up videos/photos, it won't make much
    difference.

- Version

- Just append to one logfile
- Option for quieter output, log full output to file
- Option to retrieve file lists only
- Preserve permissions/untar options
