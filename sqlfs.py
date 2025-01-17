import os
import stat
import time
import errno
import pymysql
import pymysql.cursors
import sqlite3
import hashlib
import pyfuse3


if hasattr(time, 'time_ns'):
    _timestamp_ns = time.time_ns
else:
    def _timestamp_ns():
        return int(time.time() * 1e9)


class Database:

    def __init__(self, db_path, key=None):
        user,a=db_path.split(":", maxsplit=1)
        password,a=a.split("@", maxsplit=1)
        host, database=a.split("/", maxsplit=1)
        self.conn = pymysql.connect(host=host, password=password, user=user, database=database,cursorclass=pymysql.cursors.DictCursor)
        self.conn.row_factory = sqlite3.Row
        self.init_tables(key)

    def init_tables(self, key=None):
        if key is not None:
            # hash it for sqli prevention
            key = hashlib.md5(bytes(key, 'utf8')).hexdigest()
            self.conn.execute(f'PRAGMA key=\'{key}\'')

        # create tables
        self.conn.executescript(
            '''
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS inode (
                id INTEGER PRIMARY KEY,
                uid INTEGER NOT NULL,
                gid INTEGER NOT NULL,
                mode INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                atime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                target BLOB DEFAULT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                rdev INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS link (
                id INTEGER PRIMARY KEY,
                inode INTEGER NOT NULL
                    REFERENCES inode(id) ON DELETE CASCADE,
                parent_inode INTEGER NOT NULL
                   REFERENCES inode(id) ON DELETE RESTRICT,
                name BLOB NOT NULL,
                UNIQUE (parent_inode, name)
            );
            CREATE TABLE IF NOT EXISTS block (
                inode INTEGER NOT NULL
                    REFERENCES inode(id) ON DELETE CASCADE,
                idx INTEGER NOT NULL,
                data BLOB NOT NULL,
                PRIMARY KEY (inode, idx)
            ) WITHOUT ROWID;
            '''
        )

        # create root inode
        now_ns = _timestamp_ns()
        self.conn.execute(
            '''
            INSERT OR IGNORE INTO inode (
                id, uid, gid, mode, mtime_ns, atime_ns, ctime_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                1,
                os.getuid(),
                os.getgid(),
                stat.S_IFDIR | 0o755,
                now_ns,
                now_ns,
                now_ns
            )
        )
        self.conn.executemany(
            '''
            INSERT OR IGNORE INTO link (
                inode, parent_inode, name
            ) VALUES (?, ?, ?)
            ''', [
                (1, 1, b'.'),
                (1, 1, b'..'),
            ]
        )

    def get_inode_from_id(self, inode):
        return self.conn.execute(
            '''
            SELECT *,
                (SELECT COUNT(*) FROM link WHERE inode=inode.id) AS nlink,
                (SELECT COUNT(*) FROM link WHERE parent_inode=inode.id) AS nchild,
                (SELECT COUNT(*) FROM block WHERE inode=inode.id) AS nblock
            FROM inode
            WHERE id=?
            ''',
            (inode,)
        ).fetchone()

    def get_inode_from_parent_and_name(self, parent_inode, name):
        return self.conn.execute(
            '''
            SELECT inode.*,
                (SELECT COUNT(*) FROM link WHERE inode=inode.id) AS nlink,
                (SELECT COUNT(*) FROM link WHERE parent_inode=inode.id) AS nchild,
                (SELECT COUNT(*) FROM block WHERE inode=inode.id) AS nblock,
                link.id AS link_id
            FROM inode
            INNER JOIN link ON inode.id=inode
            WHERE parent_inode=? AND name=?
            ''',
            (parent_inode, name)
        ).fetchone()

    def get_inodes_from_parent(self, parent_inode, start_id=None):
        where = ['parent_inode=?']
        params = [parent_inode]
        if start_id is not None:
            where.append('link.id>?')
            params.append(start_id)
        where = ' AND '.join(where)
        return self.conn.execute(
            f'''
            SELECT inode.*,
                (SELECT COUNT(*) FROM link WHERE inode=inode.id) AS nlink,
                (SELECT COUNT(*) FROM link WHERE parent_inode=inode.id) AS nchild,
                (SELECT COUNT(*) FROM block WHERE inode=inode.id) AS nblock,
                link.id AS link_id,
                name
            FROM inode
            INNER JOIN link ON inode.id=inode
            WHERE {where}
            ORDER BY link.id
            ''',
            params
        )

    def get_blocks(self, inode, first_idx, last_idx):
        return self.conn.execute(
            '''
            SELECT *
            FROM block
            WHERE inode=? AND idx>=? AND idx<=?
            ''',
            (inode, first_idx, last_idx)
        )

    def get_stats(self):
        return self.conn.execute(
            '''
            SELECT
                (SELECT COUNT(*) FROM block) AS f_blocks,
                (SELECT COUNT(*) FROM inode) AS f_files
            '''
        ).fetchone()

    def create_link(self, inode, parent_inode, name, is_dir=False):
        values = [(inode, parent_inode, name)]
        if is_dir:
            values.extend([
                (inode, inode, b'.'),
                (parent_inode, inode, b'..'),
            ])
        self.conn.executemany(
            '''
            INSERT INTO link (
                inode, parent_inode, name
            ) VALUES (?, ?, ?)
            ''',
            values
        )
        return inode

    def create_inode(self, parent_inode, name, uid, gid, mode, **kwargs):
        now_ns = _timestamp_ns()
        cols = ['uid', 'gid', 'mode', 'mtime_ns', 'atime_ns', 'ctime_ns']
        vals = ['?', '?', '?', '?', '?', '?']
        params = [uid, gid, mode, now_ns, now_ns, now_ns]
        for col, param in kwargs.items():
            cols.append(col)
            vals.append('?')
            params.append(param)
        cols = ','.join(cols)
        vals = ','.join(vals)
        inode = self.conn.execute(
            f'''
            INSERT INTO inode (
                {cols}
            ) VALUES ({vals})
            ''',
            params
        ).lastrowid
        return self.create_link(inode, parent_inode, name, stat.S_ISDIR(mode))

    def _update_stmts(self, **kwargs):
        stmts, params = [], []
        for col, param in kwargs.items():
            stmts.append(f'{col}=?')
            params.append(param)
        return stmts, params

    def update_inode(self, inode, **kwargs):
        if kwargs:
            stmts, params = self._update_stmts(**kwargs)
            params.append(inode)
            stmt = ','.join(stmts)
            self.conn.execute(
                f'''
                UPDATE inode
                SET {stmt}
                WHERE id=?
                ''',
                params
            )

    def update_link(self, link, **kwargs):
        if kwargs:
            stmts, params = self._update_stmts(**kwargs)
            params.append(link)
            stmt = ','.join(stmts)
            self.conn.execute(
                f'''
                UPDATE link
                SET {stmt}
                WHERE id=?
                ''',
                params
            )

    def update_blocks(self, blocks):
        self.conn.executemany(
            '''
            INSERT OR REPLACE INTO block (
                inode, idx, data
            ) VALUES (?, ?, ?)
            ''',
            blocks
        )

    def delete_link(self, link):
        self.conn.execute(
            '''
            DELETE FROM link
            WHERE id=?
            ''',
            (link,)
        )

    def delete_link_dir(self, inode):
        self.conn.execute(
            '''
            DELETE FROM link
            WHERE inode=?1 OR (
                parent_inode=?1 AND (
                    name=X'2E' OR
                    name=X'2E2E'
                )
            )
            ''',
            (inode,)
        )

    def delete_inode(self, inode):
        self.conn.execute(
            '''
            DELETE FROM inode
            WHERE id=?
            ''',
            (inode,)
        )

    def truncate_blocks(self, inode, idx):
        self.conn.execute(
            '''
            DELETE FROM block
            WHERE inode=? AND idx>=?
            ''',
            (inode, idx)
        )

    def cleanup_inodes(self):
        self.conn.execute(
            '''
            DELETE FROM inode
            WHERE inode.id IN (
                SELECT a.id
                FROM (
                    SELECT inode.id, link.inode FROM inode
                    LEFT JOIN link ON link.inode=inode.id
                ) a
                INNER JOIN
                (
                    SELECT inode.id, link.inode FROM inode
                    LEFT JOIN link ON link.parent_inode=inode.id
                ) b
                ON a.id=b.id
                WHERE a.inode IS NULL AND b.inode IS NULL
            )
            '''
        )

    def vacuum(self):
        self.conn.execute('VACUUM')

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.cleanup_inodes()
        self.commit()
        self.vacuum()
        self.conn.close()


class Operations(pyfuse3.Operations):

    blksize = 4096
    blkmask = blksize - 1
    blkshft = blkmask.bit_length()

    def __init__(self, db_path, key=None):
        super().__init__()
        self.db_path = db_path
        self.db = Database(self.db_path, key=key)

    def _to_entry(self, row):
        entry = pyfuse3.EntryAttributes()
        entry.st_ino = row['id']
        entry.st_mode = row['mode']
        entry.st_nlink = row['nlink']
        entry.st_uid = row['uid']
        entry.st_gid = row['gid']
        entry.st_rdev = row['rdev']
        entry.st_size = row['size']
        entry.st_blksize = self.blksize
        entry.st_blocks = row['nblock']
        entry.st_atime_ns = row['atime_ns']
        entry.st_mtime_ns = row['mtime_ns']
        entry.st_ctime_ns = row['ctime_ns']
        return entry

    def _get_entry(self, inode):
        row = self.db.get_inode_from_id(inode)
        if not row:
            raise pyfuse3.FUSEError(errno.EINVAL)
        return self._to_entry(row)

    async def access(self, inode, mode, ctx):
        return True

    def _create(self, parent_inode, name, uid, gid, mode, **kwargs):
        inode = self.db.create_inode(parent_inode, name, uid, gid, mode, **kwargs)
        self.db.commit()
        return self._get_entry(inode)

    async def create(self, parent_inode, name, mode, flags, ctx):
        entry = self._create(parent_inode, name, ctx.uid, ctx.gid, mode)
        return pyfuse3.FileInfo(fh=entry.st_ino), entry

    async def getattr(self, inode, ctx):
        return self._get_entry(inode)

    async def link(self, inode, new_parent_inode, new_name, ctx):
        inode = self.db.create_link(inode, new_parent_inode, new_name)
        self.db.commit()
        return self._get_entry(inode)

    async def lookup(self, parent_inode, name, ctx):
        row = self.db.get_inode_from_parent_and_name(parent_inode, name)
        if not row:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._to_entry(row)

    async def mkdir(self, parent_inode, name, mode, ctx):
        return self._create(parent_inode, name, ctx.uid, ctx.gid, mode)

    async def mknod(self, parent_inode, name, mode, rdev, ctx):
        return self._create(parent_inode, name, ctx.uid, ctx.gid, mode, rdev=rdev)

    async def open(self, inode, flags, ctx):
        if flags & os.O_TRUNC:
            self.db.truncate_blocks(inode, 0)
            self.db.update_inode(inode, size=0)
            self.db.commit()
        return pyfuse3.FileInfo(fh=inode)

    async def opendir(self, inode, ctx):
        return inode

    async def read(self, fh, off, size):
        row = self.db.get_inode_from_id(fh)
        if not row:
            raise pyfuse3.FUSEError(errno.EINVAL)
        inode_size = row['size']
        if size == 0 or off >= inode_size:
            return b''
        size = min(size, inode_size - off)
        f_idx0, f_idxn = off, off + size - 1
        b_idx0, b_idxn = f_idx0 >> self.blkshft, f_idxn >> self.blkshft
        b_cnt = b_idxn - b_idx0 + 1
        buf = bytearray(b_cnt << self.blkshft)
        for block in self.db.get_blocks(fh, b_idx0, b_idxn):
            data = block['data']
            buf_idx = (block['idx'] - b_idx0) << self.blkshft
            buf[buf_idx:buf_idx + len(data)] = data
        f_aln0 = f_idx0 & self.blkmask
        return bytes(buf[f_aln0:f_aln0 + size])

    async def readdir(self, fh, start_id, token):
        for row in self.db.get_inodes_from_parent(fh, start_id):
            entry = self._to_entry(row)
            if not pyfuse3.readdir_reply(token, row['name'], entry, row['link_id']):
                break

    async def readlink(self, inode, ctx):
        row = self.db.get_inode_from_id(inode)
        if not row:
            raise pyfuse3.FUSEError(errno.EINVAL)
        if not stat.S_ISLNK(row['mode']):
            raise pyfuse3.FUSEError(errno.EINVAL)
        return row['target']

    async def rename(self, parent_inode_old, name_old, parent_inode_new, name_new, flags, ctx):
        inode_moved = self.db.get_inode_from_parent_and_name(parent_inode_old, name_old)
        if not inode_moved:
            raise pyfuse3.FUSEError(errno.EINVAL)
        inode_deref = self.db.get_inode_from_parent_and_name(parent_inode_new, name_new)
        if inode_deref:
            if flags & pyfuse3.RENAME_NOREPLACE:
                raise pyfuse3.FUSEError(errno.EEXIST)
            elif flags & pyfuse3.RENAME_EXCHANGE:
                self.db.update_link(inode_deref['link_id'], inode=inode_moved['id'])
                self.db.update_link(inode_moved['link_id'], inode=inode_deref['id'])
                self.db.commit()
            else:
                if inode_deref['nchild']:
                    raise pyfuse3.FUSEError(errno.ENOTEMPTY)
                self.db.update_link(inode_deref['link_id'], inode=inode_moved['id'])
                self.db.delete_link(inode_moved['link_id'])
                # need to delete inode - read doco its confusing for now just
                # cleanup orphaned inodes on umount
                self.db.commit()
        else:
            self.db.update_link(inode_moved['link_id'], parent_inode=parent_inode_new, name=name_new)
            self.db.commit()

    async def rmdir(self, parent_inode, name, ctx):
        row = self.db.get_inode_from_parent_and_name(parent_inode, name)
        if not stat.S_ISDIR(row['mode']):
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if row['nchild'] > 2:
            raise pyfuse3.FUSEError(errno.ENOTEMPTY)
        self.db.delete_link_dir(row['id'])
        # need to delete inode - for now just cleanup orphaned inodes on umount
        self.db.commit()

    async def setattr(self, inode, attr, fields, fh, ctx):
        update_kwargs = {}
        if fields.update_size:
            update_kwargs['size'] = attr.st_size
            block_idx = attr.st_size >> self.blkshft
            self.db.truncate_blocks(inode, block_idx + 1)
        if fields.update_mode:
            update_kwargs['mode'] = attr.st_mode
        if fields.update_uid:
            update_kwargs['uid'] = attr.st_uid
        if fields.update_gid:
            update_kwargs['gid'] = attr.st_gid
        if fields.update_mtime:
            update_kwargs['mtime_ns'] = attr.st_mtime_ns
        if fields.update_atime:
            update_kwargs['atime_ns'] = attr.st_atime_ns
        if fields.update_ctime:
            update_kwargs['ctime_ns'] = attr.st_ctime_ns
        else:
            update_kwargs['ctime_ns'] = _timestamp_ns()
        self.db.update_inode(inode, **update_kwargs)
        self.db.commit()
        return self._get_entry(inode)

    @staticmethod
    def _memfree():
        with open('/proc/meminfo') as fd:
            for line in fd:
                parts = line.split()
                if parts[0] == 'MemFree:':
                    return int(parts[1]) * 1024

    async def statfs(self, ctx):
        stats = self.db.get_stats()

        # base it off free memory
        if self.db_path == ':memory:':
            memfree = self._memfree()
            f_bfree = memfree >> self.blkshft
            f_bsize = self.blksize
            f_bavail = f_bfree
            f_ffree = f_bfree
            f_favail = f_ffree

        # base it of free disk
        else:
            real = os.statvfs(self.db_path)
            f_bfree = real.f_bfree
            f_bsize = real.f_bsize
            f_bavail = real.f_bavail
            f_ffree = real.f_ffree
            f_favail = real.f_favail

        ours = pyfuse3.StatvfsData()
        ours.f_bsize = self.blksize
        ours.f_frsize = self.blksize
        ours.f_blocks = stats['f_blocks']
        ours.f_files = stats['f_files']
        ours.f_bfree = (f_bfree * f_bsize) >> self.blkshft
        ours.f_bavail = (f_bavail * f_bsize) >> self.blkshft
        ours.f_ffree = f_ffree
        ours.f_favail = f_favail
        # just set it (theres no real limit)
        ours.f_namemax = 255
        return ours

    async def symlink(self, parent_inode, name, target, ctx):
        mode = stat.S_IFLNK | 0o777
        return self._create(parent_inode, name, ctx.uid, ctx.gid, mode, size=len(target), target=target)

    async def unlink(self, parent_inode, name, ctx):
        row = self.db.get_inode_from_parent_and_name(parent_inode, name)
        if stat.S_ISDIR(row['mode']):
            raise pyfuse3.FUSEError(errno.EISDIR)
        self.db.delete_link(row['link_id'])
        # need to delete inode - for now just cleanup orphaned inodes on umount
        self.db.commit()

    def _blocks(self, buf, inode, b_idx0):
        idx = b_idx0
        for i in range(0, len(buf), self.blksize):
            block = bytes(buf[i:i + self.blksize]).rstrip(b'\x00')
            yield inode, idx, block
            idx += 1

    async def write(self, fh, off, buf):
        row = self.db.get_inode_from_id(fh)
        if not row:
            raise pyfuse3.FUSEError(errno.EINVAL)
        size = len(buf)
        if not size:
            return 0
        f_end = off + size
        f_idx0, f_idxn = off, f_end - 1
        f_aln0, f_alnn = off & self.blkmask, f_end & self.blkmask
        b_idx0, b_idxn = f_idx0 >> self.blkshft, f_idxn >> self.blkshft
        b_cnt = b_idxn - b_idx0 + 1
        _buf = bytearray(b_cnt << self.blkshft)
        if f_aln0:
            for block in self.db.get_blocks(fh, b_idx0, b_idx0):
                data = block['data']
                _buf[:len(data)] = data
        if f_alnn:
            for block in self.db.get_blocks(fh, b_idxn, b_idxn):
                data = block['data']
                _buf[-self.blksize:len(data) - self.blksize] = data
        _buf[f_aln0:f_aln0 + len(buf)] = buf
        self.db.update_blocks(self._blocks(memoryview(_buf), fh, b_idx0))
        if f_end > row['size']:
            now_ns = _timestamp_ns()
            self.db.update_inode(fh, size=f_end, ctime_ns=now_ns, mtime_ns=now_ns)
        self.db.commit()
        return size

    def close(self):
        self.db.close()
