"""Server-only credentials. No environment-key input or public serialization."""
import os
import stat
import sys
import tomllib
from pathlib import Path

MAX_CONFIG_BYTES = 64 * 1024
TEMPLATE = '# Server-only settings; edit locally, then restart Verinote.\n[openrouter]\napi_key = ""\n\n[dictionary]\nrelation_type_warning_threshold = 20\n'
UNSUPPORTED = '이 플랫폼에서는 설정 파일 접근 권한을 검증할 수 없습니다. 파일 없이 CLI를 사용하거나 POSIX/WSL 환경을 사용하세요.'


class ConfigError(ValueError):
    """A static diagnostic with no source text or underlying exception attached."""


class Credentials:
    __slots__ = ('__openrouter_key', 'relation_type_warning_threshold')

    def __init__(self, openrouter_key='', *, relation_type_warning_threshold=20):
        from .dictionary_type_warnings import validate_threshold
        self.relation_type_warning_threshold = validate_threshold(relation_type_warning_threshold)
        self.__openrouter_key = openrouter_key

    def __repr__(self):
        return f'Credentials(openrouter_configured={bool(self.__openrouter_key)})'

    def __reduce_ex__(self, protocol):
        raise TypeError('Credentials는 직렬화할 수 없습니다. public_status()를 사용하세요.')

    def public_status(self):
        return {'openrouter': {'configured': bool(self.__openrouter_key)}}

    def openrouter_key(self):
        """Explicit server-memory injection for the future HTTP adapter only."""
        return self.__openrouter_key


def default_config_path():
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        xdg = os.environ.get('XDG_CONFIG_HOME', '')
        base = Path(xdg) if os.path.isabs(xdg) else Path.home() / '.config'
    return base / 'verinote' / 'config.toml'


def _path(path):
    # Do not resolve symlinks before O_NOFOLLOW can reject the selected file.
    return Path(os.path.abspath((default_config_path() if path is None else Path(path)).expanduser()))


def _supported():
    return sys.platform != 'win32' and os.name == 'posix' and all(
        hasattr(os, flag) for flag in ('O_NOFOLLOW', 'O_NONBLOCK', 'O_DIRECTORY'))


def _check_file(info):
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError('설정은 심볼릭 링크나 특수 파일이 아닌 일반 파일이어야 합니다.')
    if info.st_uid != os.getuid():
        raise ConfigError('설정 파일은 현재 사용자 소유여야 합니다.')
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ConfigError('설정 파일 권한은 0600이어야 합니다. 소유자와 경로를 확인한 뒤 chmod 600으로 수정하세요.')


def _load(path):
    selected = _path(path)
    if not _supported():
        if path is None:
            try:
                selected.lstat()
            except FileNotFoundError:
                return Credentials()
        raise ConfigError(UNSUPPORTED)
    try:
        fd = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if path is None:
            return Credentials()
        raise ConfigError('지정한 설정 파일이 없습니다. --init-config로 먼저 생성하세요.') from None
    try:
        info = os.fstat(fd)
        _check_file(info)
        if info.st_size > MAX_CONFIG_BYTES:
            raise ConfigError('설정 파일은 64 KiB 이하여야 합니다.')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError('설정 파일은 64 KiB 이하여야 합니다.')
    value = tomllib.loads(raw.decode('utf-8'))
    provider = value.get('openrouter', {})
    dictionary = value.get('dictionary', {})
    if (set(value) - {'openrouter', 'dictionary'} or not isinstance(provider, dict) or set(provider) - {'api_key'}
            or not isinstance(dictionary, dict) or set(dictionary) - {'relation_type_warning_threshold'}):
        raise ConfigError('설정에는 [openrouter].api_key와 [dictionary].relation_type_warning_threshold만 사용할 수 있습니다.')
    key = provider.get('api_key', '')
    if not isinstance(key, str):
        raise ConfigError('openrouter.api_key는 문자열이어야 합니다.')
    from .dictionary_type_warnings import validate_threshold
    try:
        threshold = validate_threshold(dictionary.get('relation_type_warning_threshold', 20))
    except ValueError:
        raise ConfigError('dictionary.relation_type_warning_threshold는 1~200 정수여야 합니다.') from None
    return Credentials(key.strip(), relation_type_warning_threshold=threshold)


def load_config(path=None):
    """Validate once at startup; missing default/empty key means unconfigured."""
    try:
        return _load(path)
    except ConfigError as exc:
        message = str(exc)
    except (UnicodeError, tomllib.TOMLDecodeError, RecursionError):
        message = '설정 파일은 올바른 UTF-8 TOML이어야 합니다. 파일 내용을 로컬에서 확인하세요.'
    except (OSError, ValueError, RuntimeError):
        message = '설정 파일을 안전하게 읽을 수 없습니다. 경로·소유자·권한과 심볼릭 링크 여부를 확인하세요.'
    # Raising outside except also removes raw TOML/OS errors from __context__.
    raise ConfigError(message)


def _directory(path):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except FileNotFoundError:
        pass
    parent = _directory(path.parent)
    try:
        created = False
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
        fd = os.open(path.name, flags, dir_fd=parent)
        if created:
            try:
                if os.fstat(fd).st_uid != os.getuid():
                    raise ConfigError('새 설정 디렉터리의 소유자를 확인할 수 없습니다.')
                os.fchmod(fd, 0o700)
            except Exception:
                os.close(fd)
                raise
        return fd
    finally:
        os.close(parent)


def _create(path):
    selected = _path(path)
    if not _supported():
        raise ConfigError(UNSUPPORTED)
    parent = _directory(selected.parent)
    try:
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ConfigError('생성할 설정 파일의 부모 디렉터리는 현재 사용자 소유이며 그룹/다른 사용자 쓰기가 금지되어야 합니다.')
        try:
            fd = os.open(selected.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
        except FileExistsError:
            raise ConfigError('설정 파일이 이미 존재합니다. 기존 파일은 덮어쓰지 않습니다.') from None
        try:
            # Only the newly and exclusively created descriptor is adjusted.
            os.fchmod(fd, 0o600)
            _check_file(os.fstat(fd))
            with os.fdopen(fd, 'w', encoding='utf-8', closefd=False) as stream:
                stream.write(TEMPLATE)
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    return selected


def create_config(path=None):
    """Create an empty, private template without overwriting existing paths."""
    try:
        return _create(path)
    except ConfigError as exc:
        message = str(exc)
    except (OSError, ValueError, RuntimeError):
        message = '설정 파일을 안전하게 생성할 수 없습니다. 경로·소유자·권한과 심볼릭 링크 여부를 확인하세요.'
    raise ConfigError(message)
