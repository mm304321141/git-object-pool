#!/usr/bin/env python3
"""Regression tests for controlled object-pool GC. No network or user-pool access."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().with_name('git-pool-wrapper.py')
SPEC = importlib.util.spec_from_file_location('git_pool_wrapper', SCRIPT)
WRAPPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRAPPER)
REAL_GIT = shutil.which('git')
REAL_ADD_GC_PROTECT_REFS = WRAPPER.add_gc_protect_refs


class PoolGCTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='git-pool-gc-test-')
        self.addCleanup(self.temp.cleanup)
        self.pool_dir = Path(self.temp.name)
        self.pool = self.pool_dir / 'pool.git'
        self.pool.mkdir()
        self.log = self.pool / 'gc.log'
        self.stamp = self.pool_dir / '.gc_last_run'
        self.log.write_text('warning: There are too many unreachable loose objects\n')
        self.previous_stamp = str(time.time())
        self.stamp.write_text(self.previous_stamp)
        self.events = []
        patches = {
            'POOL_DIR': self.pool_dir,
            'POOL_PATH': self.pool,
            'POOL_LOCK_PATH': self.pool_dir / '.lock',
            'REGISTRY_FILE': self.pool_dir / '.registered_shells',
            'GC_LAST_RUN_FILE': self.stamp,
            '_fetched_urls': set(),
            'registered_shells': mock.Mock(return_value=['/work/.git']),
            'pool_dependents': mock.Mock(return_value=['/work/.git']),
            'all_shells_enumerable': mock.Mock(return_value=True),
            'count_pool_objects': mock.Mock(return_value=10),
            'collect_shell_live_commits': mock.Mock(return_value={'a' * 40}),
            'collect_repo_ref_tips': mock.Mock(return_value=[]),
            'cleanup_object_pool_refs': mock.Mock(side_effect=lambda *_: self.events.append('cleanup')),
            'add_gc_protect_refs': mock.Mock(side_effect=lambda *_: self.events.append('protect') or 1),
            'run_git': mock.Mock(side_effect=self.fake_git),
        }
        self.patch = mock.patch.multiple(WRAPPER, **patches)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def fake_git(self, args, **kwargs):
        command = next((s for s in ('fetch', 'repack', 'prune', 'gc', 'update-ref', 'cat-file')
                        if s in args), 'other')
        self.events.append(command)
        return subprocess.CompletedProcess(args, 0, '', '')

    def run_gc(self, args=None):
        with contextlib.redirect_stderr(io.StringIO()):
            WRAPPER.gc_pool_repos(args)

    def assert_failure_preserved(self):
        self.assertTrue(self.log.exists())
        self.assertEqual(self.stamp.read_text(), self.previous_stamp)

    def test_old_log_bypasses_recent_success_and_gc_finishes_in_one_pass(self):
        self.run_gc()
        self.assertEqual(self.events, ['cleanup', 'protect', 'repack', 'prune', 'cleanup'])
        self.assertFalse(self.log.exists())
        self.assertNotEqual(self.stamp.read_text(), self.previous_stamp)

    def test_without_old_log_recent_success_is_rate_limited(self):
        self.log.unlink()
        self.run_gc()
        self.assertEqual(self.events, [])
        self.assertEqual(self.stamp.read_text(), self.previous_stamp)

    def test_expired_interval_runs_without_log(self):
        self.log.unlink()
        self.stamp.write_text('0')
        self.run_gc()
        self.assertIn('prune', self.events)

    def test_prune_never_still_repacks_and_clears_old_log(self):
        self.run_gc(['--prune=never'])
        self.assertNotIn('prune', self.events)
        self.assertIn('repack', self.events)
        self.assertFalse(self.log.exists())

    def test_quiet_aggressive_flags_apply_to_repack(self):
        self.run_gc(['--quiet', '--aggressive'])
        repack = next(call.args[0] for call in WRAPPER.run_git.call_args_list
                      if 'repack' in call.args[0])
        self.assertIn('-q', repack)
        self.assertIn('-f', repack)

    def test_analysis_failure_does_not_mutate_objects_or_clear_log(self):
        for name, value in (('all_shells_enumerable', False),
                            ('count_pool_objects', None),
                            ('collect_shell_live_commits', None)):
            with self.subTest(name=name), mock.patch.object(WRAPPER, name, return_value=value):
                with self.assertRaises(WRAPPER.GitCommandError):
                    self.run_gc()
                self.assert_failure_preserved()
                self.assertEqual(self.events, [])

    def test_protection_failure_stops_before_repack_and_prune(self):
        WRAPPER.add_gc_protect_refs.side_effect = WRAPPER.GitCommandError('ref failure')
        with self.assertRaises(WRAPPER.GitCommandError):
            self.run_gc()
        self.assertEqual(self.events, ['cleanup', 'cleanup'])
        self.assert_failure_preserved()

    def test_repack_failure_stops_before_prune_and_preserves_log(self):
        def failing(args, **kwargs):
            if 'repack' in args:
                self.events.append('repack')
                raise WRAPPER.GitCommandError('repack failure')
            return self.fake_git(args, **kwargs)
        WRAPPER.run_git.side_effect = failing
        with self.assertRaises(WRAPPER.GitCommandError):
            self.run_gc()
        self.assertNotIn('prune', self.events)
        self.assertEqual(self.events[-1], 'cleanup')
        self.assert_failure_preserved()

    def test_prune_failure_preserves_log_and_allows_immediate_retry(self):
        def failing(args, **kwargs):
            if 'prune' in args:
                raise WRAPPER.GitCommandError('prune failure')
            return self.fake_git(args, **kwargs)
        WRAPPER.run_git.side_effect = failing
        with self.assertRaises(WRAPPER.GitCommandError):
            self.run_gc()
        self.assert_failure_preserved()
        WRAPPER.run_git.side_effect = self.fake_git
        self.events.clear()
        self.run_gc()
        self.assertIn('prune', self.events)
        self.assertFalse(self.log.exists())

    def test_no_dependents_uses_non_pruning_gc(self):
        WRAPPER.pool_dependents.return_value = []
        self.run_gc()
        args = WRAPPER.run_git.call_args.args[0]
        self.assertIn('gc', args)
        self.assertIn('--prune=never', args)
        self.assertFalse(self.log.exists())

    def test_no_dependents_gc_failure_keeps_log(self):
        WRAPPER.pool_dependents.return_value = []
        WRAPPER.run_git.side_effect = WRAPPER.GitCommandError('gc failure')
        with self.assertRaises(WRAPPER.GitCommandError):
            self.run_gc()
        self.assert_failure_preserved()

    def test_log_unlink_failure_returns_error(self):
        original_unlink = Path.unlink
        def fail_log(path, *args, **kwargs):
            if path == self.log:
                raise PermissionError('test denied')
            return original_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', fail_log):
            with self.assertRaises(WRAPPER.GitCommandError):
                self.run_gc()
        self.assert_failure_preserved()

    def test_pool_remote_fetch_disables_automatic_maintenance_per_call(self):
        WRAPPER._fetch_pool_remote('origin', 'https://example.invalid/repo')
        args = WRAPPER.run_git.call_args.args[0]
        self.assertIn('gc.auto=0', args)
        self.assertIn('maintenance.auto=false', args)
        self.assertEqual(args.count('-c'), 2)
        self.assertIn('fetch', args)

    def test_local_to_pool_fetch_disables_automatic_maintenance_per_call(self):
        with mock.patch.object(WRAPPER, 'replace_alternates'):
            self.assertTrue(WRAPPER._fetch_local_to_pool('/work/.git', self.pool))
        args = WRAPPER.run_git.call_args.args[0]
        self.assertIn('gc.auto=0', args)
        self.assertIn('maintenance.auto=false', args)
        self.assertIn('HEAD:refs/object-pool/HEAD', args)

    def test_gc_failure_does_not_fallthrough_to_successful_native_gc(self):
        with mock.patch.object(WRAPPER, 'gc_pool_repos',
                               side_effect=WRAPPER.GitCommandError('pool failure')):
            with mock.patch.object(WRAPPER, '_exec_passthrough') as passthrough:
                with self.assertRaises(WRAPPER.GitCommandError):
                    WRAPPER.do_gc_command([], [])
                passthrough.assert_not_called()

    def test_protect_ref_update_failure_is_fatal(self):
        real_function = REAL_ADD_GC_PROTECT_REFS
        def git_result(args, **kwargs):
            if 'update-ref' in args:
                self.assertTrue(kwargs.get('check', True))
                raise WRAPPER.GitCommandError('cannot write protect ref')
            return self.fake_git(args, **kwargs)
        WRAPPER.run_git.side_effect = git_result
        with self.assertRaises(WRAPPER.GitCommandError):
            real_function(self.pool, {'a' * 40})


@unittest.skipUnless(REAL_GIT, 'native Git required')
class PoolGCIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='git-pool-gc-integration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith('GIT_'):
                self.env.pop(key)
        # Isolate all configuration, credentials, templates and pool state.
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / '.config'),
                        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TEMPLATE_DIR=str(self.home), GIT_POOL=str(self.root / 'pool'),
                        SYSTEM_GIT=REAL_GIT, PYTHONDONTWRITEBYTECODE='1',
                        LC_ALL='C')
        self.work = self.root / 'work'
        self.pool = self.root / 'pool' / 'pool.git'
        self.git('init', str(self.work))
        self.git('-C', str(self.work), 'config', 'user.name', 'Pool Test')
        self.git('-C', str(self.work), 'config', 'user.email', 'pool-test@example.invalid')
        (self.work / 'data.txt').write_text('must remain accessible via pool\n')
        self.git('-C', str(self.work), 'add', 'data.txt')
        self.git('-C', str(self.work), 'commit', '-m', 'local-only commit')
        self.live = self.git('-C', str(self.work), 'rev-parse', 'HEAD').stdout.strip()
        self.invoke_wrapper('migrate', '--no-recursive')

    def git(self, *args, input=None, check=True):
        result = subprocess.run([REAL_GIT, *args], env=self.env, text=True,
                                input=input, capture_output=True)
        if check and result.returncode:
            self.fail(f'git {args}: {result.stderr}')
        return result

    def invoke_wrapper(self, *args, check=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.work,
                                env=self.env, text=True, capture_output=True)
        if check and result.returncode:
            self.fail(f'wrapper {args}: {result.stderr}')
        return result

    def invoke_library(self, expression):
        code = ('import importlib.util; '
                f'spec=importlib.util.spec_from_file_location("wrapper", {str(SCRIPT)!r}); '
                'w=importlib.util.module_from_spec(spec); spec.loader.exec_module(w); '
                + expression)
        result = subprocess.run([sys.executable, '-c', code], cwd=self.work,
                                env=self.env, text=True, capture_output=True)
        if result.returncode:
            self.fail(result.stderr)
        return result

    def test_pool_fetch_does_not_run_auto_gc_with_existing_config_and_log(self):
        prefix = ('--git-dir', str(self.pool))
        self.git(*prefix, 'config', 'gc.auto', '1')
        self.git(*prefix, 'config', 'maintenance.auto', 'true')
        # Two loose objects in the sampled bucket force the native auto-GC heuristic.
        import hashlib
        count = 0
        for i in range(10000):
            data = f'garbage {i}\n'
            payload = data.encode()
            sha = hashlib.sha1(b'blob ' + str(len(payload)).encode() + b'\0' + payload).hexdigest()
            if sha.startswith('17'):
                self.git(*prefix, 'hash-object', '-w', '--stdin', input=data)
                count += 1
                if count == 2:
                    break
        self.assertEqual(count, 2)
        log = self.pool / 'gc.log'
        warning = 'warning: There are too many unreachable loose objects\n'
        log.write_text(warning)
        # Show that the fixture would reproduce the user's warning without the fix.
        baseline = self.git(*prefix, 'gc', '--auto')
        self.assertIn('last gc run', baseline.stderr)
        remote_fetch = self.invoke_library(f'w.ensure_pool_bare_repo({str(self.work)!r})')
        self.assertNotIn('last gc run', remote_fetch.stderr)
        self.assertEqual(log.read_text(), warning)
        # The local-to-pool call is also isolated from automatic maintenance.
        self.invoke_library(f'assert w._fetch_local_to_pool({str(self.work / ".git")!r}, w.POOL_PATH)')
        self.assertEqual(log.read_text(), warning)
        self.assertEqual(self.git(*prefix, 'config', '--local', '--get', 'gc.auto').stdout.strip(), '1')
        self.assertEqual(self.git(*prefix, 'config', '--local', '--get', 'maintenance.auto').stdout.strip(), 'true')
        self.invoke_wrapper('gc')
        self.assertFalse(log.exists())
        self.assertNotIn('last gc run', self.git(*prefix, 'gc', '--auto').stderr)

    def test_no_dependents_preserves_unreachable_objects_and_clears_log(self):
        prefix = ('--git-dir', str(self.pool))
        trash = self.git(*prefix, 'hash-object', '-w', '--stdin', input='keep without registry\n').stdout.strip()
        (self.pool.parent / '.registered_shells').write_text('')
        log = self.pool / 'gc.log'
        log.write_text('old warning\n')
        result = self.invoke_wrapper('gc')
        self.assertIn('无 worktree 依赖', result.stderr)
        self.assertFalse(log.exists())
        self.git(*prefix, 'cat-file', '-e', trash)
        self.git(*prefix, 'cat-file', '-e', self.live)

    def test_one_gc_removes_packed_and_loose_garbage_preserves_borrowed_objects(self):
        prefix = ('--git-dir', str(self.pool))
        packed_trash = self.git(*prefix, 'hash-object', '-w', '--stdin',
                               input='packed garbage\n').stdout.strip()
        self.git(*prefix, 'update-ref', 'refs/test/live', self.live)
        self.git(*prefix, 'update-ref', 'refs/test/trash', packed_trash)
        self.git(*prefix, 'repack', '-ad')
        self.git(*prefix, 'update-ref', '-d', 'refs/test/live')
        self.git(*prefix, 'update-ref', '-d', 'refs/test/trash')
        loose_trash = self.git(*prefix, 'hash-object', '-w', '--stdin',
                              input='loose garbage\n').stdout.strip()
        # Both garbage kinds exist, but the live commit has no permanent pool ref.
        self.git(*prefix, 'cat-file', '-e', packed_trash)
        self.assertFalse((self.pool / 'objects' / packed_trash[:2] / packed_trash[2:]).exists())
        self.assertTrue((self.pool / 'objects' / loose_trash[:2] / loose_trash[2:]).exists())
        self.assertEqual(self.git(*prefix, 'for-each-ref', '--format=%(refname)').stdout, '')
        self.git('-C', str(self.work), 'show', 'HEAD:data.txt')
        # Native repack keeps loose copies; remove only this isolated fixture's
        # redundant loose files so that the test genuinely relies on alternates.
        for directory in (self.work / '.git' / 'objects').iterdir():
            if len(directory.name) == 2 and all(c in '0123456789abcdef' for c in directory.name):
                for loose in directory.iterdir():
                    self.git(*prefix, 'cat-file', '-e', directory.name + loose.name)
                    loose.unlink()
        self.assertFalse((self.work / '.git' / 'objects' / self.live[:2] / self.live[2:]).exists())
        self.assertFalse(list((self.work / '.git' / 'objects' / 'pack').glob('*.pack')))
        log = self.pool / 'gc.log'
        log.write_text('warning: There are too many unreachable loose objects; run git prune\n')
        stamp = self.pool.parent / '.gc_last_run'
        stamp.write_text(str(time.time()))
        result = self.invoke_wrapper('gc')
        self.assertIn('gc-protect:', result.stderr)
        self.assertNotIn('跳过本次全池 GC', result.stderr)
        self.assertFalse(log.exists())
        for sha in (packed_trash, loose_trash):
            self.assertNotEqual(self.git(*prefix, 'cat-file', '-e', sha, check=False).returncode, 0)
        self.git(*prefix, 'cat-file', '-e', self.live)
        self.git('-C', str(self.work), 'fsck', '--connectivity-only', '--no-reflogs')
        self.assertEqual(self.git('-C', str(self.work), 'show', 'HEAD:data.txt').stdout,
                         'must remain accessible via pool\n')
        self.assertEqual(self.git(*prefix, 'for-each-ref', '--format=%(refname)',
                                 'refs/object-pool/').stdout, '')
        # A normal subsequent fetch remains warning-free; no persistent GC config change.
        self.git('-C', str(self.work), 'remote', 'add', 'origin', str(self.work))
        fetched = self.invoke_wrapper('fetch', 'origin')
        self.assertNotIn('last gc run', fetched.stderr)
        self.assertNotIn('too many unreachable', fetched.stderr)
        self.assertNotEqual(self.git(*prefix, 'config', '--local', '--get',
                                    'gc.auto', check=False).returncode, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
