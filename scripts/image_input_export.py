"""Root-side ownership of one dedicated AstrBot attachment directory."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat

NAMES=re.compile(r'(?:[0-9a-f]{64}\.(?:png|jpg|gif|webp)|\.hd-input-[0-9a-f]{32}\.tmp)\Z')


def require(value):
    if not value:raise ValueError('image_export_directory')


@contextmanager
def managed_files(fd,uids):
    """Pin and validate the entire bounded inventory before changing any file."""
    items=[]
    try:
        names=os.listdir(fd)
        require(len(names)<=512)
        for name in names:
            require(NAMES.fullmatch(name))
            item=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
            items.append(item)
            value=os.fstat(item)
            require(stat.S_ISREG(value.st_mode) and value.st_nlink==1
                    and value.st_uid in uids and stat.S_IMODE(value.st_mode)==0o600
                    and value.st_size<=25*1024*1024)
        yield items
    finally:
        for item in items:os.close(item)


def discard_pending_record(record):
    temporary=record.with_name('.'+record.name+'.next')
    try:info=temporary.lstat()
    except FileNotFoundError:return
    require(stat.S_ISREG(info.st_mode) and info.st_nlink==1
            and info.st_uid in {0,os.geteuid()} and stat.S_IMODE(info.st_mode)==0o600
            and info.st_size<=65536)
    temporary.unlink()


def prepare(path,owner):
    path=Path(path).absolute()
    require(all(not part.is_symlink() for part in (path,*path.parents)))
    require(path.parent.parent.is_dir())
    path.parent.mkdir(mode=0o700,exist_ok=True)
    record=owner.STATE/'image-export-owner.json'
    previous=owner.read_json(record) if record.exists() else None
    uid=os.geteuid()
    if previous is not None:
        require(set(previous)=={'schemaVersion','uid'} and type(previous['schemaVersion']) is int
                and previous['schemaVersion']==1 and type(previous['uid']) is int and previous['uid']>1)
        uid=previous['uid']
    path.mkdir(mode=0o700,exist_ok=True)
    info=path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid in {os.geteuid(),uid} and stat.S_IMODE(info.st_mode)==0o700)
    # Lock new workers out until their exact dynamic UID has been verified.
    os.chown(path,os.geteuid(),-1)
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        require(info.st_uid==os.geteuid() and stat.S_IMODE(info.st_mode)==0o700)
        with managed_files(fd,{os.geteuid(),uid}) as items:
            # The old journal remains valid throughout interrupted normalization.
            for item in items:os.fchown(item,os.geteuid(),-1)
        # A failed atomic write can be discarded only once every file is ours.
        discard_pending_record(record)
    finally:os.close(fd)
    return path,uid


def assign(path,previous_uid,uid,owner):
    require(type(uid) is int and uid>1)
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        require(info.st_uid==os.geteuid() and stat.S_IMODE(info.st_mode)==0o700)
        with managed_files(fd,{os.geteuid()}) as items:
            # All files are ours before committing the next UID. Interrupted
            # assignment then leaves only controller/new-UID files to normalize.
            owner.write_json(owner.STATE/'image-export-owner.json',{'schemaVersion':1,'uid':uid})
            for item in items:os.fchown(item,uid,-1)
        os.fchown(fd,uid,-1)
    finally:os.close(fd)
