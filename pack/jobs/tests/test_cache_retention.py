"""Q15: production cleanup on original, non-game directory fixtures."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import library


class CompilerRetentionTests(unittest.TestCase):
    def _preflight_fixture(self, root):
        import os
        import time
        c = SimpleNamespace(private=root / 'private', work_root=root / 'private', library=root / 'library', emit=lambda *a, **k: None)
        c.library.mkdir()
        paths = []
        for i in range(4):
            platform = c.private / f'run/s2arm/cache/direct/{i:016X}/store'
            path = platform / 'dshb/aa' / ('aa' + 'f' * 62 + '.e')
            path.parent.mkdir(parents=True)
            path.write_bytes(b'original synthetic cold payload')
            old = time.time() - 7200 - i * 100
            os.utime(path, (old, old))
            os.utime(platform, (old, old))
            paths.append(path)
        game = {'id': '0000000000000000', 'packages': []}
        return c, paths, game

    def test_preflight_reclaims_fourth_title_to_actual_need(self):
        from unittest.mock import patch
        import cache_retention
        with tempfile.TemporaryDirectory() as directory:
            c, paths, game = self._preflight_fixture(Path(directory))
            def usage(path):
                return SimpleNamespace(free=(12 if paths[3].exists() else 30) << 30)
            with patch.object(library, 'selected_packages', return_value=[{'sha256': 'synthetic', 'size': 4 << 30}]), \
                 patch('shutil.disk_usage', side_effect=usage):
                budget = library.check_pack_space(c, game, compiler_policy=cache_retention.retention_policy())
            # 4 GiB × 1.1 + 15 GB，独立手算
            self.assertEqual(budget['compilerMinFreeBytes'], (4 << 30) * 11 // 10 + 15_000_000_000)
            self.assertTrue(all(p.exists() for p in paths[:3]))
            self.assertFalse(paths[3].exists())

    def test_preflight_still_full_reports_logical_cache_and_clear_action(self):
        from unittest.mock import patch
        import cache_retention
        from core import Failure
        with tempfile.TemporaryDirectory() as directory:
            c, paths, game = self._preflight_fixture(Path(directory))
            with patch.object(library, 'selected_packages', return_value=[{'sha256': 'synthetic', 'size': 4 << 30}]), \
                 patch('shutil.disk_usage', return_value=SimpleNamespace(free=12 << 30)):
                with self.assertRaises(Failure) as error:
                    library.check_pack_space(c, game, compiler_policy=cache_retention.retention_policy())
            self.assertEqual(error.exception.code, 'disk_full')
            self.assertNotIn('逻辑字节', str(error.exception))   # 玩家提示不露内部词
            self.assertTrue(all(p.exists() for p in paths[:3]))
            self.assertFalse(paths[3].exists())

    def test_preflight_does_not_evict_for_another_volume(self):
        from unittest.mock import patch
        import cache_retention
        from core import Failure
        with tempfile.TemporaryDirectory() as directory:
            c, paths, game = self._preflight_fixture(Path(directory))
            real_stat = Path.stat
            def stat(path, *args, **kwargs):
                info = real_stat(path, *args, **kwargs)
                if path == c.library:
                    return SimpleNamespace(st_dev=info.st_dev + 1)
                return info
            budget = {'volumes': [{'role': 'library', 'needBytes': 18 << 30,
                                   'freeBytes': 12 << 30, 'enough': False}]}
            with patch.object(library, 'space_budget', return_value=budget), \
                 patch.object(Path, 'stat', stat), \
                 patch('shutil.disk_usage', return_value=SimpleNamespace(free=1 << 30)):
                with self.assertRaises(Failure):
                    library.check_pack_space(c, game, compiler_policy=cache_retention.retention_policy())
            self.assertTrue(all(p.exists() for p in paths), 'another volume cannot reclaim this cache volume')

    def test_success_cleanup_keeps_compiler_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            c = SimpleNamespace(private=root / 'private', work_root=root / 'private', library=root / 'library')
            files = [c.private / 'cache/air/ios/aa' / ('a' * 64 + '.air'),
                     c.private / 'cache/pso/bb' / ('b' * 64 + '.bin'),
                     c.private / 'run/gal_dl_cache/ios/cc' / ('c' * 64 + '.metallib')]
            for p in files:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b'original synthetic compiler cache')
            scratch = c.library / 'iPhone/0100000000000000/extract/synthetic'
            scratch.parent.mkdir(parents=True)
            scratch.write_bytes(b'discard')
            library.prune_build(c, '0100000000000000')
            self.assertEqual([p.read_bytes() for p in files if p.exists()],
                             [b'original synthetic compiler cache'] * 3)
            self.assertFalse(scratch.exists())

    def test_success_keeps_direct_store_and_removes_only_transients(self):
        # S2Arm 10-31: the direct cache is store/ (shard files) + logs/; the former per-key trees ios/ and mac/ are the old layout, cleared like work/
        with tempfile.TemporaryDirectory() as directory:
            c = SimpleNamespace(private=Path(directory) / 'private', work_root=Path(directory) / 'private', library=Path(directory) / 'library')
            gid = '0100000000000000'
            direct = c.private / 'run/s2arm/cache/direct' / gid
            kept = [direct / 'store/dshb/aa' / ('aa' + '0' * 62 + '.e'), direct / 'logs/build.log', direct / 'future-format/unknown',
                    direct / ('mac.stage.' + str(__import__('os').getpid())) / 'active']
            gone = [direct / 'work/log', direct / 'merged/pkg/input', direct / 'ios.stage.2147483647/partial',
                    direct / 'ios/b-123456789abc/batch.metallib', direct / 'mac/cas/unity/ios/aa' / ('a' * 64 + '.metallib')]
            for path in kept + gone:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'original synthetic payload')
            library.prune_build(c, gid)
            self.assertTrue(all(p.exists() for p in kept))
            self.assertEqual([p.read_bytes() for p in kept], [b'original synthetic payload'] * len(kept))
            self.assertTrue(all(not p.exists() for p in gone))
            library.prune_build(c, gid, retain_compiler=False)
            self.assertFalse(direct.exists(), 'explicit clear preserves full-clear semantics')

    def test_prune_keeps_romfs_store_and_delete_removes_read_only_store(self):
        # 11-03 pack/roots.sh：现行版本的 romfs 指向 roots/.romfs_store/<款>/<身份>（整棵只读）。成功后清理只删旧版本、不删存储；删除游戏能删掉只读存储
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            c = SimpleNamespace(private=Path(directory) / 'private', work_root=Path(directory) / 'private', library=Path(directory) / 'library', begin=lambda *a, **k: None)
            gid = '0100000000000000'
            roots = c.library / 'iPhone' / gid / 'roots'
            ident = roots / '.romfs_store/g/s0-1'
            (ident / 'sub').mkdir(parents=True)
            (ident / 'sub/a.bin').write_bytes(b'original synthetic romfs')
            for name in ('g@new', 'g@old'):
                (roots / name).mkdir()
                (roots / name / 'romfs').symlink_to('../.romfs_store/g/s0-1')
            (roots / 'g@现行').symlink_to('g@new')
            for p in (ident / 'sub/a.bin', ident / 'sub', ident):
                os.chmod(p, 0o555 if p.is_dir() else 0o444)
            (roots.parent / 't.ipa').write_bytes(b'ipa')   # 老玩家：有 IPA 没容器件，现行产物根要留着
            library.prune_build(c, gid)
            self.assertFalse((roots / 'g@old').exists())
            self.assertEqual((roots / 'g@现行/romfs/sub/a.bin').read_bytes(), b'original synthetic romfs')
            with patch.object(library, 'game_caches', return_value=[]):
                library._rm(c, c.library, c.library / 'iPhone' / gid)
            self.assertFalse((c.library / 'iPhone' / gid).exists())

    def test_transient_links_cannot_delete_retained_platforms_or_other_title(self):
        from unittest.mock import patch
        for name in ('work', 'merged', 'ios.stage.12345'):
            for destination in ('store', 'logs', '../0100000000000001/store'):
                with self.subTest(name=name, destination=destination), tempfile.TemporaryDirectory() as directory:
                    c = SimpleNamespace(private=Path(directory) / 'private', work_root=Path(directory) / 'private', library=Path(directory) / 'library')
                    direct = c.private / 'run/s2arm/cache/direct/0100000000000000'
                    direct.mkdir(parents=True)
                    target = direct / destination
                    target.mkdir(parents=True, exist_ok=True)
                    payload = target / 'retained.metallib'
                    payload.write_bytes(b'never delete retained payload')
                    link = direct / name
                    link.symlink_to(destination, target_is_directory=True)
                    # Only mock a nonexistent stage PID; no process is signalled.
                    with patch.object(library.os, 'kill', side_effect=ProcessLookupError):
                        library.prune_build(c, '0100000000000000')
                    self.assertTrue(payload.exists())
                    self.assertEqual(payload.read_bytes(), b'never delete retained payload')
                    self.assertTrue(link.is_symlink())

    def test_link_replacement_after_selection_is_rechecked_at_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            c = SimpleNamespace(private=Path(directory) / 'private', work_root=Path(directory) / 'private', library=Path(directory) / 'library')
            direct = c.private / 'run/s2arm/cache/direct/0100000000000000'
            work = direct / 'work'
            work.mkdir(parents=True)
            selected = library.direct_transients(c, direct)
            self.assertIn(work, selected)
            target = direct / 'store'
            target.mkdir()
            payload = target / 'retained.metallib'
            payload.write_bytes(b'keep')
            work.rmdir()
            work.symlink_to('store', target_is_directory=True)
            self.assertFalse(library._rm_direct_transient(c, direct, work))
            self.assertTrue(payload.exists())
            self.assertEqual(payload.read_bytes(), b'keep')
            self.assertTrue(work.is_symlink())


class BudgetTests(unittest.TestCase):
    NOW = 20000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name) / 'private'

    def put(self, relative, data=b'1234', age=7200):
        import os
        p = self.private / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (self.NOW - age, self.NOW - age))
        return p

    def trim(self, free=0, readings=None, **kwargs):
        from cache_retention import trim
        oracle = iter(readings) if readings is not None else None
        return trim(self.private, min_free_bytes=1, now=self.NOW,
                    free_bytes=(lambda: next(oracle)) if oracle else (lambda: free), **kwargs)

    def test_low_disk_evicts_oldest_and_stops_when_real_free_reaches_floor(self):
        air = self.put('cache/air/ios/aa/' + 'a' * 64 + '.air', age=9000)
        pso = self.put('cache/pso/bb/' + 'b' * 64 + '.bin', age=8000)
        dl = self.put('run/gal_dl_cache/macos/cc/' + 'c' * 64 + '.metallib', age=7000)
        result = self.trim(readings=[0, 1])
        self.assertFalse(air.exists())
        self.assertEqual(pso.read_bytes(), b'1234')
        self.assertEqual(dl.read_bytes(), b'1234')
        self.assertEqual((result['observedBytes'], result['retainedBytes'], result['removedBytes']), (12, 8, 4))
        self.assertEqual((result['removedEntries'], result['pressureRemaining']), (1, False))
        self.assertEqual(self.trim(free=1)['removedEntries'], 0)

    def test_hot_future_temporary_and_unknown_are_retained_above_budget(self):
        files = [self.put('cache/air/ios/aa/' + 'a' * 64 + '.air', age=3599),
                 self.put('cache/pso/bb/' + 'b' * 64 + '.bin', age=-1),
                 self.put('cache/pso/cc/' + 'c' * 64 + '.bin.123.tmp'),
                 self.put('cache/air/future/aa/' + 'a' * 64 + '.air'),
                 self.put('cache/air/ios/ff/' + 'a' * 64 + '.air'),
                 self.put('cache/pso/bb/readme')]
        result = self.trim(0)
        self.assertTrue(all(p.exists() for p in files))
        self.assertEqual(result['removedEntries'], 0)
        self.assertEqual(result['retainedBytes'], 24)

    def test_batch_layouts_are_eligible_but_unrelated_payloads_are_not(self):
        air = self.put('cache/air/ios/uaa/' + 'a' * 64 + '.air')
        dl = self.put('run/gal_dl_cache/macos/bbb/' + 'b' * 64 + '.metallib')
        unknown = self.put('cache/pso/cc/extra/' + 'c' * 64 + '.bin')
        result = self.trim(0)
        self.assertFalse(air.exists())
        self.assertFalse(dl.exists())
        self.assertTrue(unknown.exists())
        self.assertEqual(result['retainedBytes'], 4)

    def test_symlinks_never_followed_or_removed(self):
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        target = outside / ('a' * 64 + '.air')
        target.write_bytes(b'outside')
        root = self.private / 'cache/air'
        root.mkdir(parents=True)
        (root / 'ios').symlink_to(outside, target_is_directory=True)
        (self.private / 'cache/pso').symlink_to(outside, target_is_directory=True)
        (root / 'link').symlink_to(target)
        self.assertEqual(self.trim(0)['removedEntries'], 0)
        self.assertEqual(target.read_bytes(), b'outside')
        self.assertTrue((root / 'link').is_symlink())

    def test_refresh_between_scan_and_unlink_is_retained(self):
        from unittest.mock import patch
        import os
        p = self.put('cache/pso/aa/' + 'a' * 64 + '.bin')
        real_lstat = Path.lstat
        calls = []
        def refreshing_lstat(path):
            if path == p:
                calls.append(path)
                if len(calls) == 2:
                    os.utime(p, (self.NOW, self.NOW))
            return real_lstat(path)
        with patch.object(Path, 'lstat', refreshing_lstat):
            result = self.trim(0)
        self.assertTrue(p.exists(), 'refreshed completed entry must survive trimming')
        self.assertEqual(p.read_bytes(), b'1234')
        self.assertEqual(p.stat().st_mtime, self.NOW)
        self.assertEqual(result['changedEntries'], 1)
        self.assertEqual(result['removedEntries'], 0)

    def direct(self, gid, kind='unity', key='a', age=7200):   # one entry file of the title's store (C# dsh_batch build: store/dshb/<two hex>/<sha256>.e; kind: what it holds, for the reader)
        import os
        p = self.put('run/s2arm/cache/direct/' + gid + '/store/dshb/' + key * 2 + '/' + key * 64 + '.e', age=age)
        base = self.private / 'run/s2arm/cache/direct' / gid / 'store'
        os.utime(base, (self.NOW - age, self.NOW - age))
        return p

    def test_older_direct_cas_is_eligible_only_beyond_kept_titles(self):
        a = self.direct('0100000000000000', age=9000)
        b = self.direct('0100000000000001', kind='metallib', key='b', age=8000)
        generic = self.put('cache/pso/cc/' + 'c' * 64 + '.bin', age=7000)
        unknown = self.put('run/s2arm/cache/direct/0100000000000000/work/log', data=b'not compiler bytes')
        result = self.trim(readings=[0, 1], keep_titles=1)
        self.assertEqual((result['observedBytes'], result['removedBytes']), (12, 4))
        self.assertFalse(a.exists())
        self.assertTrue(b.exists() and generic.exists() and unknown.exists())

    def test_direct_layout_entries_and_former_shards(self):
        # 11-01: an entry is store/dshb/<its first two hex>/<sha256>.e; a former Python store shard store/<two hex>.pack has no reader and is reclaimable;
        # an entry filed under the wrong subdirectory, or with another name, is not an entry of this producer and stays
        import os
        base = 'run/s2arm/cache/direct/0100000000000000/store/'
        entry = self.put(base + 'dshb/ab/ab' + '0' * 62 + '.e')
        shard = self.put(base + 'cd.pack')
        misfiled = self.put(base + 'dshb/ef/ab' + '1' * 62 + '.e')
        other = self.put(base + 'dshb/ab/ab' + '2' * 62 + '.metallib')
        os.utime(self.private / base, (1, 1))
        newer = self.direct('0100000000000001', key='c', age=-1)   # the newer title holds the single protected slot
        result = self.trim(0, keep_titles=1)
        self.assertFalse(entry.exists() or shard.exists())
        self.assertTrue(misfiled.exists() and other.exists() and newer.exists())
        self.assertEqual(result['removedEntries'], 2)

    def test_successful_title_is_hot_even_when_dmsh_did_not_touch_entries(self):
        from cache_retention import trim
        gid = '0100000000000000'
        p = self.direct(gid)
        before = p.stat().st_mtime
        result = trim(self.private, min_free_bytes=1, free_bytes=lambda: 0, keep_titles=1, now=self.NOW, current_gid=gid)
        self.assertTrue(p.exists())
        self.assertEqual(p.stat().st_mtime, before)
        self.assertEqual(result['retainedBytes'], 4)
        # A newer title occupies the single protected slot; previous title still
        # has its one-hour safety window on top of recent-title protection.
        self.direct('0100000000000001', key='b', age=-1)
        result = trim(self.private, min_free_bytes=1, free_bytes=lambda: 0, keep_titles=1, now=self.NOW + 3599)
        self.assertTrue(p.exists())
        result = trim(self.private, min_free_bytes=1, free_bytes=lambda: 0, keep_titles=1, now=self.NOW + 3600)
        self.assertFalse(p.exists())

    def test_direct_unknown_store_file_is_counted_but_not_evicted(self):
        import os
        base = self.private / 'run/s2arm/cache/direct/0100000000000000/store'
        p = self.put(str(base.relative_to(self.private)) + '/dshb/aa/' + 'a' * 64 + '.e.tmp123.4')   # an entry being written (Store.Put: temporary file, then rename): not an entry
        os.utime(base, (1, 1))
        result = self.trim(0)
        self.assertEqual(result['observedBytes'], 4)
        self.assertEqual(result['retainedBytes'], 4)
        self.assertTrue(p.exists())

    def test_unreadable_root_and_nested_directory_are_explicitly_incomplete(self):
        import os
        from unittest.mock import patch
        p = self.put('cache/pso/aa/' + 'a' * 64 + '.bin')
        real_scandir = os.scandir
        for blocked in (self.private / 'cache/pso', p.parent):
            with self.subTest(blocked=blocked):
                def denied(path):
                    if Path(path) == blocked:
                        raise PermissionError(13, 'synthetic scan refusal', str(path))
                    return real_scandir(path)
                with patch.object(os, 'scandir', denied):
                    result = self.trim(100)
                self.assertFalse(result['accountingComplete'])
                self.assertEqual(result['unscannedRoots'], 1)
                self.assertEqual(result['scanErrors'], 1)
                self.assertEqual(result['errors'][0]['errno'], 13)
                self.assertEqual(p.read_bytes(), b'1234')

    def test_stat_errors_are_retained_and_reported(self):
        from unittest.mock import patch
        p = self.put('cache/pso/aa/' + 'a' * 64 + '.bin')
        real_lstat = Path.lstat
        for blocked in (self.private / 'cache', p.parent, p):
            with self.subTest(blocked=blocked):
                def denied(path):
                    if path == blocked:
                        raise PermissionError(13, 'synthetic stat refusal', str(path))
                    return real_lstat(path)
                with patch.object(Path, 'lstat', denied):
                    result = self.trim(0)
                self.assertFalse(result['accountingComplete'])
                self.assertGreaterEqual(result['scanErrors'], 1)
                self.assertEqual(p.read_bytes(), b'1234')

    def test_eviction_error_preserves_payload_and_is_not_hidden(self):
        from unittest.mock import patch
        p = self.put('cache/pso/aa/' + 'a' * 64 + '.bin')
        real_unlink = Path.unlink
        def denied(path, *args, **kwargs):
            if path == p:
                raise PermissionError(13, 'synthetic eviction refusal', str(path))
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', denied):
            result = self.trim(0)
        self.assertEqual(p.read_bytes(), b'1234')
        self.assertEqual((result['removedEntries'], result['evictionErrors'], result['retainedBytes']), (0, 1, 4))
        self.assertEqual(result['errors'], [dict(operation='evict', errno=13, path=str(p.relative_to(self.private)))])

    def test_environment_policy_is_explicit_and_old_byte_limit_is_not_a_trigger(self):
        import os
        from unittest.mock import patch
        from cache_retention import retention_policy, KEEP_TITLES, MIN_FREE_BYTES
        with patch.dict(os.environ, {'S2A_COMPILER_CACHE_MAX_BYTES': '0'}, clear=True):
            self.assertEqual(retention_policy(), dict(keep_titles=KEEP_TITLES, min_free_bytes=MIN_FREE_BYTES))
        with patch.dict(os.environ, {'S2A_COMPILER_CACHE_KEEP_TITLES': '5', 'S2A_COMPILER_CACHE_MIN_FREE_BYTES': '0'}):
            self.assertEqual(retention_policy(), dict(keep_titles=5, min_free_bytes=0))
        for name, values in [('S2A_COMPILER_CACHE_KEEP_TITLES', ['', '0', '-1', '1.0']),
                             ('S2A_COMPILER_CACHE_MIN_FREE_BYTES', ['', '-1', '1GiB'])]:
            for value in values:
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}), self.assertRaises(ValueError):
                    retention_policy()

    def test_invalid_policy_rejected_before_touching_files(self):
        from cache_retention import trim
        for value in (-1, True, 0.5, '8'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                trim(self.private, min_free_bytes=value)

    def test_recent_two_large_sparse_titles_stay_whole_after_hot_window(self):
        from cache_retention import trim
        paths = [self.direct('0100000000000000', age=9000),
                 self.direct('0100000000000001', key='b', age=8000)]
        import os
        for p in paths:
            with p.open('r+b') as f:
                f.truncate(9 * 1024**3)
            os.utime(p, (1, 1))
        for free in (0, 100 * 1024**3):
            result = trim(self.private, now=self.NOW, free_bytes=lambda: free)
            self.assertEqual(len(result['protectedTitles']), 2)
            self.assertEqual(result['removedEntries'], 0)
            self.assertEqual([p.stat().st_size for p in paths], [9 * 1024**3] * 2)

    def test_low_disk_never_sacrifices_latest_three_titles(self):
        paths = [self.direct(f'{n:016X}', key=hex(n)[2:], age=10000-n) for n in range(1, 5)]
        result = self.trim(0)
        self.assertFalse(paths[0].exists())
        self.assertTrue(all(p.exists() for p in paths[1:]))
        self.assertEqual(result['protectedTitles'], [f'{n:016X}' for n in (2, 3, 4)])
        self.assertTrue(result['pressureRemaining'], 'do not claim logical unlink reclaimed physical space')
        self.assertEqual(result['freeBytes'], 0)

    def test_sufficient_free_space_never_trims_cold_old_titles(self):
        paths = [self.direct(f'{n:016X}', key=hex(n)[2:], age=10000-n) for n in range(1, 5)]
        result = self.trim(1)
        self.assertTrue(all(p.exists() for p in paths))
        self.assertEqual(result['removedEntries'], 0)

    def test_unknown_disk_free_is_reported_and_never_causes_deletion(self):
        from cache_retention import trim
        p = self.put('cache/pso/aa/' + 'a' * 64 + '.bin')
        def unavailable():
            raise OSError(5, 'synthetic disk reading failure')
        result = trim(self.private, now=self.NOW, free_bytes=unavailable)
        self.assertTrue(p.exists())
        self.assertFalse(result['accountingComplete'])
        self.assertEqual(result['diskErrors'], 1)
        self.assertIsNone(result['pressureRemaining'])


class ExecutionLockTests(unittest.TestCase):
    def test_ipa_uses_existing_library_execution_lock_and_releases_on_failure(self):
        from unittest.mock import patch
        from core import Context, Failure
        import contextlib
        import io
        import ipa
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            request = dict(schema=1, taskId='cache-lock', operation='setup',
                           engineDir=str(Path(__file__).resolve().parents[3]),
                           privateRoot=str(root / 'private'), library=str(root / 'library'))
            c = Context(request)
            c.r['libraryId'] = library.setup(c)['libraryId']
            other = Context(dict(c.r, taskId='other'))
            def fail_while_locked(actual):
                self.assertIs(actual, c)
                with self.assertRaises(Failure):
                    with other.lock('execution'):
                        self.fail('concurrent library execution entered')
                raise RuntimeError('synthetic build failure')
            with patch.object(ipa, '_build', fail_while_locked):
                with self.assertRaisesRegex(RuntimeError, 'synthetic build failure'):
                    ipa.build(c)
            with other.lock('execution'):
                pass


if __name__ == '__main__':
    unittest.main()
