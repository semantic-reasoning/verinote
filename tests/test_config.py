import contextlib
import io
import json
import os
import pickle
import stat
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from verinote import config
from verinote.config import ConfigError, Credentials, create_config, load_config
from verinote.server import Server, main

SENTINEL = 'synthetic-private-config-value'


class ConfigPaths(unittest.TestCase):
    def test_platform_defaults_and_relative_xdg_ignored(self):
        with patch('verinote.config.Path.home', return_value=Path('/home/example')):
            with patch('verinote.config.sys.platform', 'darwin'):
                self.assertEqual(config.default_config_path(), Path('/home/example/Library/Application Support/verinote/config.toml'))
            with patch('verinote.config.sys.platform', 'linux'):
                for value, base in [('', '/home/example/.config'), ('relative', '/home/example/.config'), ('/custom', '/custom')]:
                    with patch.dict(os.environ, {'XDG_CONFIG_HOME': value}):
                        self.assertEqual(config.default_config_path(), Path(base) / 'verinote/config.toml')
            with patch('verinote.config.sys.platform', 'win32'), patch.dict(os.environ, {'APPDATA': '/roaming'}):
                self.assertEqual(config.default_config_path(), Path('/roaming/verinote/config.toml'))

    def test_relative_explicit_path_is_anchored_without_resolving_links(self):
        self.assertEqual(config._path('config.toml'), Path.cwd() / 'config.toml')

    def test_credentials_have_only_explicit_secret_access(self):
        credentials = Credentials(SENTINEL)
        for public in (repr(credentials), str(credentials), json.dumps(credentials.public_status())):
            self.assertNotIn(SENTINEL, public)
        self.assertEqual(credentials.public_status(), {'openrouter': {'configured': True}})
        for serialize in (vars, json.dumps, pickle.dumps):
            with self.subTest(serialize=serialize), self.assertRaises(TypeError) as caught:
                serialize(credentials)
            self.assertNotIn(SENTINEL, str(caught.exception))
        consumer = Mock(side_effect=lambda injected: injected.openrouter_key())
        self.assertEqual(consumer(credentials), SENTINEL)
        self.assertFalse(Credentials().public_status()['openrouter']['configured'])


@unittest.skipUnless(os.name == 'posix', 'POSIX file permission tests')
class ConfigFiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'config.toml'

    def write(self, text, mode=0o600):
        if self.path.exists():
            self.path.chmod(0o600)
        self.path.write_text(text, encoding='utf-8')
        self.path.chmod(mode)
        return self.path

    def assert_safe_error(self, operation):
        try:
            operation()
        except ConfigError as exc:
            self.assertNotIn(SENTINEL, str(exc))
            self.assertNotIn(SENTINEL, repr(exc))
            self.assertNotIn(SENTINEL, ''.join(traceback.format_exception(exc)))
            self.assertIsNone(exc.__cause__)
            self.assertIsNone(exc.__context__)
            self.assertFalse(hasattr(exc, 'doc'))
            return str(exc)
        self.fail('ConfigError was not raised')

    def test_valid_trimmed_key_and_no_environment_mutation(self):
        self.write(f'[openrouter]\napi_key = "  {SENTINEL}  "\n')
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'ignored-env'}):
            self.assertEqual(load_config(self.path).openrouter_key(), SENTINEL)
            self.assertEqual(os.environ['OPENROUTER_API_KEY'], 'ignored-env')

    def test_missing_default_is_unconfigured_and_environment_key_ignored(self):
        with patch('verinote.config.default_config_path', return_value=self.path), patch.dict(os.environ, {'OPENROUTER_API_KEY': SENTINEL}):
            self.assertFalse(load_config().public_status()['openrouter']['configured'])
        self.assert_safe_error(lambda: load_config(self.path))

    def test_empty_missing_key_and_empty_template_are_unconfigured(self):
        for value in ('', '[openrouter]\n', '[openrouter]\napi_key = "  "\n', config.TEMPLATE):
            with self.subTest(value=value):
                self.write(value)
                self.assertFalse(load_config(self.path).public_status()['openrouter']['configured'])

    def test_invalid_toml_unicode_and_types_do_not_retain_source_exception(self):
        for value in (f'[openrouter]\napi_key = "{SENTINEL}', f'{SENTINEL} = [',
                      '[openrouter]\napi_key = 123', 'openrouter = "wrong"',
                      'openrouter = ["wrong"]', f'[unexpected]\n{SENTINEL} = 1',
                      f'[openrouter]\n{SENTINEL} = true'):
            with self.subTest(value=value):
                self.write(value)
                self.assert_safe_error(lambda: load_config(self.path))
        self.path.write_bytes(b'\xff' + SENTINEL.encode())
        self.assert_safe_error(lambda: load_config(self.path))

    def test_read_failure_is_sanitized_without_fallback(self):
        self.write(config.TEMPLATE)
        with patch('verinote.config.os.open', side_effect=PermissionError(SENTINEL)), patch.dict(os.environ, {'OPENROUTER_API_KEY': SENTINEL}):
            self.assert_safe_error(lambda: load_config(self.path))

    def test_unsafe_modes_and_owner_fail_before_content_read_without_chmod(self):
        for mode in (0o644, 0o640, 0o660, 0o400, 0o4600):
            with self.subTest(mode=oct(mode)):
                self.write(config.TEMPLATE, mode)
                with patch('verinote.config.os.fdopen') as read:
                    self.assert_safe_error(lambda: load_config(self.path))
                    read.assert_not_called()
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), mode)
        self.write(config.TEMPLATE)
        original = self.path.stat()
        wrong = SimpleNamespace(st_mode=original.st_mode, st_uid=os.getuid()+1, st_size=original.st_size)
        with patch('verinote.config.os.fstat', return_value=wrong), patch('verinote.config.os.fdopen') as read:
            self.assert_safe_error(lambda: load_config(self.path))
            read.assert_not_called()

    def test_symlink_dangling_symlink_directory_and_fifo_rejected(self):
        target = self.root / 'target.toml'
        target.write_text(config.TEMPLATE)
        target.chmod(0o600)
        self.path.symlink_to(target)
        self.assert_safe_error(lambda: load_config(self.path))
        target.unlink()
        self.assert_safe_error(lambda: load_config(self.path))
        self.path.unlink()
        self.path.mkdir()
        self.assert_safe_error(lambda: load_config(self.path))
        self.path.rmdir()
        os.mkfifo(self.path, 0o600)
        self.assert_safe_error(lambda: load_config(self.path))

    def test_size_limit_checks_stat_and_actual_bounded_read(self):
        self.write('#' + 'x' * config.MAX_CONFIG_BYTES)
        self.assert_safe_error(lambda: load_config(self.path))
        info = self.path.stat()
        inaccurate = SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid, st_size=0)
        with patch('verinote.config.os.fstat', return_value=inaccurate):
            self.assert_safe_error(lambda: load_config(self.path))

    def test_read_uses_validated_descriptor_even_when_path_is_replaced(self):
        self.write(f'[openrouter]\napi_key = "{SENTINEL}"\n')
        original_fstat = os.fstat
        def replace_after_open(fd):
            info = original_fstat(fd)
            self.path.rename(self.root / 'old.toml')
            self.write('[openrouter]\napi_key = "replacement"\n')
            return info
        with patch('verinote.config.os.fstat', side_effect=replace_after_open):
            self.assertEqual(load_config(self.path).openrouter_key(), SENTINEL)
        self.assertEqual(load_config(self.path).openrouter_key(), 'replacement')

    def test_template_creation_modes_at_open_and_new_directories(self):
        selected = self.root / 'new' / 'nested' / 'config.toml'
        original_open = os.open
        calls = []
        def observe(path, flags, mode=0o777, **kwargs):
            fd = original_open(path, flags, mode, **kwargs)
            if flags & os.O_CREAT:
                calls.append((flags, mode, stat.S_IMODE(os.fstat(fd).st_mode)))
            return fd
        previous_umask = os.umask(0)
        try:
            with patch('verinote.config.os.open', side_effect=observe):
                self.assertEqual(create_config(selected), selected)
        finally:
            os.umask(previous_umask)
        self.assertEqual(calls[0][1:], (0o600, 0o600))
        self.assertTrue(calls[0][0] & os.O_EXCL)
        self.assertTrue(calls[0][0] & os.O_NOFOLLOW)
        for directory in (selected.parent, selected.parent.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(selected.stat().st_mode), 0o600)
        self.assertEqual(selected.read_text(), config.TEMPLATE)
        self.assertFalse(load_config(selected).public_status()['openrouter']['configured'])

    def test_creation_never_overwrites_files_links_or_changes_existing_parent(self):
        self.root.chmod(0o750)
        self.write(SENTINEL)
        self.assert_safe_error(lambda: create_config(self.path))
        self.assertEqual(self.path.read_text(), SENTINEL)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o750)
        self.path.unlink()
        target = self.root / 'target'
        target.write_text(SENTINEL)
        self.path.symlink_to(target)
        self.assert_safe_error(lambda: create_config(self.path))
        self.assertEqual(target.read_text(), SENTINEL)
        self.path.unlink()
        self.root.chmod(0o770)
        self.assert_safe_error(lambda: create_config(self.path))
        self.assertFalse(self.path.exists())
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o770)

    def test_creation_rejects_wrong_parent_owner_and_parent_symlink(self):
        original = self.root.stat()
        wrong = SimpleNamespace(st_mode=original.st_mode, st_uid=os.getuid()+1)
        with patch('verinote.config.os.fstat', return_value=wrong):
            self.assert_safe_error(lambda: create_config(self.path))
        self.assertFalse(self.path.exists())
        link = self.root / 'linked'
        link.symlink_to(self.root, target_is_directory=True)
        self.assert_safe_error(lambda: create_config(link / 'config.toml'))

    def test_creation_pins_parent_descriptor_during_path_replacement(self):
        parent = self.root / 'selected'
        parent.mkdir(mode=0o700)
        pinned = self.root / 'pinned'
        original_open = os.open
        def replace_parent(path, flags, mode=0o777, **kwargs):
            if flags & os.O_CREAT:
                parent.rename(pinned)
                parent.mkdir(mode=0o700)
            return original_open(path, flags, mode, **kwargs)
        with patch('verinote.config.os.open', side_effect=replace_parent):
            create_config(parent / 'config.toml')
        self.assertFalse((parent / 'config.toml').exists())
        self.assertEqual((pinned / 'config.toml').read_text(), config.TEMPLATE)

    def test_creation_errors_do_not_retain_os_exception(self):
        with patch('verinote.config.os.open', side_effect=PermissionError(SENTINEL)):
            self.assert_safe_error(lambda: create_config(self.path))

    def test_windows_policy_refuses_file_loading_and_initialization(self):
        with patch('verinote.config.sys.platform', 'win32'), patch('verinote.config.default_config_path', return_value=self.path):
            self.assertFalse(load_config().public_status()['openrouter']['configured'])
            self.assert_safe_error(lambda: load_config(self.path))
            self.assert_safe_error(lambda: create_config(self.path))
            self.write(config.TEMPLATE)
            self.assert_safe_error(load_config)


@unittest.skipUnless(os.name == 'posix', 'POSIX startup integration')
class ConfigStartup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.toml'

    def test_init_exits_without_starting_database_or_server(self):
        with patch('verinote.server.Store') as store, patch('verinote.server.Service') as service, patch('verinote.server.Server') as server, contextlib.redirect_stdout(io.StringIO()):
            main(['--config', str(self.path), '--init-config'])
        store.assert_not_called()
        service.assert_not_called()
        server.assert_not_called()
        self.assertEqual(self.path.read_text(), config.TEMPLATE)

    def test_bad_configuration_exits_before_database_recovery_without_secret(self):
        self.path.write_text(f'[openrouter]\napi_key = "{SENTINEL}')
        self.path.chmod(0o600)
        output = io.StringIO()
        with patch('verinote.server.Store') as store, patch('verinote.server.Service') as service, contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as caught:
            main(['--config', str(self.path)])
        self.assertEqual(caught.exception.code, 2)
        store.assert_not_called()
        service.assert_not_called()
        self.assertNotIn(SENTINEL, output.getvalue())
        self.assertNotIn('Traceback', output.getvalue())

    def start_fake_server(self, args):
        with patch('verinote.server.Store') as store, patch('verinote.server.Service') as service, patch('verinote.server.Server') as server, contextlib.redirect_stdout(io.StringIO()):
            server.return_value.serve_forever.side_effect = KeyboardInterrupt
            main(args)
            store.return_value.recover.assert_called_once()
            server.return_value.server_close.assert_called_once()
            service.return_value.pool.shutdown.assert_called_once_with(wait=True)
            return server.call_args.kwargs['credentials']

    def test_missing_default_preserves_normal_startup(self):
        with patch('verinote.config.default_config_path', return_value=self.path):
            credentials = self.start_fake_server([])
        self.assertFalse(credentials.public_status()['openrouter']['configured'])

    def test_startup_injects_loaded_snapshot_without_reloading(self):
        create_config(self.path)
        self.path.write_text(f'[openrouter]\napi_key = "{SENTINEL}"\n')
        credentials = self.start_fake_server(['--config', str(self.path), '--port', '8766'])
        self.path.write_text('[openrouter]\napi_key = "changed"\n')
        self.assertEqual(credentials.openrouter_key(), SENTINEL)
        self.assertNotIn(SENTINEL, repr(credentials))

    def test_server_constructor_preserves_old_callers_and_explicit_injection(self):
        with patch('verinote.server.ThreadingHTTPServer.__init__'):
            legacy = Server(('127.0.0.1', 0), Mock(), Mock())
            self.assertFalse(legacy.credentials.public_status()['openrouter']['configured'])
            credentials = Credentials(SENTINEL)
            injected = Server(('127.0.0.1', 0), Mock(), Mock(), credentials=credentials)
            self.assertIs(injected.credentials, credentials)


if __name__ == '__main__':
    unittest.main()
