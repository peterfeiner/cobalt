import errno
import os
import pwd 
import shutil
import tempfile

from postinst import Mount, update_fstab, update_mount

def test_fstab_parsing():
    assert None == Mount.parse_line('')
    assert None == Mount.parse_line('\n')
    assert None == Mount.parse_line('# 1 2 3 4 5 6\n')
    assert None == Mount.parse_line('1 2 3 4 # 5 6\n')

    m = Mount.parse_line('1 2 3 4 5 6 # foo\n')
    assert m == Mount(dev='1', path='2', fs='3', options='4', dump='5', pass_='6')
    assert m == ('1', '2', '3', '4', '5', '6')

def test_updated_line():
    line = 'none /mnt/vmsfs vmsfs defaults 0 0 '
    m = Mount.parse_line(line)
    m = m._replace(dev='/dev/sda', fs='ext2', options='asdf')
    assert m.updated_line(line) == '/dev/sda /mnt/vmsfs ext2 asdf 0 0'
    assert m.updated_line(line + ' # foo') == '/dev/sda /mnt/vmsfs ext2 asdf 0 0 # foo'

def test_update_fstab():
    passwd = pwd.struct_passwd(['libvirt-qemu', '*', 111, 222, '',
                                '/home/nosuchuser', '/bin/bash'])

    def test(before, after=None, *updated_mountpoints):
        tmpdir = tempfile.mkdtemp()
        if after == None:
            after = before
        try:
            path = os.path.join(tmpdir, 'fstab')
            with open(path, 'w') as fstab:
                fstab.write(before)
            updated = update_fstab(path, passwd)
            with open(path) as fstab:
                assert after == fstab.read()
            assert list(updated_mountpoints) == [mount.path for mount in updated]
            # Make sure temp file from safe_writelines is cleaned up.
            assert [(tmpdir, [], ['fstab'])] == list(os.walk(tmpdir))
        finally:
            shutil.rmtree(tmpdir)

    # No vmsfs mounts. Should not change.
    test('')
    test('###')
    test('can not parse')
    test('/dev/sda1 / ext2 defaults 0 0')
    # Has everything, should not change.
    test('none /mnt vmsfs uid=1,gid=2,mode=3 0 0')
    # Noting specified.
    test('none /mnt vmsfs defaults 0 0',
         'none /mnt vmsfs defaults,uid=111,gid=222,mode=775 0 0',
         '/mnt')
    # Just missing mode.
    test('none /mnt vmsfs uid=1,gid=2 0 0',
         'none /mnt vmsfs uid=1,gid=2,mode=775 0 0',
         '/mnt')
    # Just missing gid.
    test('none /mnt vmsfs uid=1,mode=3 0 0',
         'none /mnt vmsfs uid=1,mode=3,gid=222 0 0',
         '/mnt')
    # Just missing uid.
    test('none /mnt vmsfs gid=1,mode=3 0 0',
         'none /mnt vmsfs gid=1,mode=3,uid=111 0 0',
         '/mnt')
    # Preserve comments.
    test('none /mnt vmsfs defaults 0 0 #\tcomment!!\n',
         'none /mnt vmsfs defaults,uid=111,gid=222,mode=775 0 0 #\tcomment!!\n',
         '/mnt')
    # Leading whitespace 
    test(' none /mnt vmsfs defaults 0 0\n',
         'none /mnt vmsfs defaults,uid=111,gid=222,mode=775 0 0\n',
         '/mnt')
    # Preserve trailing newline.
    test('none /mnt vmsfs defaults 0 0\n',
         'none /mnt vmsfs defaults,uid=111,gid=222,mode=775 0 0\n',
         '/mnt')
    # More than one vmsfs mount.
    test('none /mnt vmsfs defaults 1 2\n'
         'none /foo vmsfs defaults 0 0\n',
         'none /mnt vmsfs defaults,uid=111,gid=222,mode=775 1 2\n'
         'none /foo vmsfs defaults,uid=111,gid=222,mode=775 0 0\n',
         '/mnt', '/foo')

def touch(path):
    open(path, 'a').close()

def check_exists(path):
    if not os.path.exists(path):
        error = OSError()
        error.errno = errno.ENOENT
        raise error

def test_update_mount():
    own = {}
    mod = {}

    def chown(path, uid, gid):
        check_exists(path)
        path = os.path.abspath(path)
        old_uid, old_gid = own.get(path, (None, None))
        if uid == -1:
            uid = old_uid
        if gid == -1:
            gid = old_gid
        own[path] = (uid, gid)
        if (uid, gid) == (None, None):
            del own[path]

    def chmod(path, mode):
        check_exists(path)
        mod[os.path.abspath(path)] = mode

    def reset_fs():
        own.clear()
        mod.clear()

    tmpdir = tempfile.mkdtemp()
    try:
        mount = Mount('none', '', 'vmsfs', 'uid=111,gid=222,mode=775', '0', '0')

        # Nonexistent mountpoint shouldn't cause an error.
        no_dir = mount._replace(path=os.path.join(tmpdir, 'no_dir'))
        update_mount(no_dir, chown, chmod)
        assert own == {}
        assert mod == {}

        # Unmounted vmsfs shouldn't be touched at all.
        not_mounted = mount._replace(path=os.path.join(tmpdir, 'not_mounted'))
        os.mkdir(not_mounted.path)
        update_mount(not_mounted, chown, chmod)
        assert own == {}
        assert mod == {}

        # Mounted vmsfs should be updated.
        mounted = mount._replace(path=os.path.join(tmpdir, 'mounted'))
        mounted_vms_path = os.path.join(mounted.path, 'vms')
        os.mkdir(mounted.path)
        touch(mounted_vms_path)
        update_mount(mounted, chown, chmod)
        assert own == {mounted.path: (111, 222),
                       mounted_vms_path: (111, 222)}
        assert mod == {mounted.path: 0775,
                       mounted_vms_path: 0664}

        # Bad uid is ignored
        bad_uid = mounted._replace(options='uid=bad,gid=333,mode=775')
        update_mount(bad_uid, chown, chmod)
        assert own == {mounted.path: (111, 333),
                       mounted_vms_path: (111, 333)}
        assert mod == {mounted.path: 0775,
                       mounted_vms_path: 0664}

        # Bad gid is ignored
        bad_gid = mounted._replace(options='uid=444,gid=bad,mode=775')
        update_mount(bad_gid, chown, chmod)
        assert own == {mounted.path: (444, 333),
                       mounted_vms_path: (444, 333)}
        assert mod == {mounted.path: 0775,
                       mounted_vms_path: 0664}

        # Bad mode is ignored
        bad_mode = mounted._replace(options='uid=111,gid=222,mode=bad')
        update_mount(bad_mode, chown, chmod)
        assert own == {mounted.path: (111, 222),
                       mounted_vms_path: (111, 222)}
        assert mod == {mounted.path: 0775,
                       mounted_vms_path: 0664}
        
    finally:
        shutil.rmtree(tmpdir)
