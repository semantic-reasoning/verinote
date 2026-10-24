"""Check built license metadata and package data against the project source."""
import argparse
import configparser
import re
import stat
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


def require(condition, label):
    if not condition: raise ValueError(label)


def names_safe(names):
    require(len(names) == len(set(names)), 'duplicate archive member')
    for name in names:
        path = PurePosixPath(name)
        require(not path.is_absolute() and '..' not in path.parts and '\\' not in name, 'unsafe archive member')


def metadata(raw, project):
    value = BytesParser().parsebytes(raw)
    require(value.get_all('License-Expression') == ['MPL-2.0'], 'License-Expression')
    require(value.get_all('License-File') == ['LICENSE'], 'License-File')
    require(value.get('License') is None, 'legacy License metadata')
    require(not any(c.startswith('License ::') for c in value.get_all('Classifier', [])), 'License classifier')
    for field, expected in [('Name', project['name']), ('Version', project['version']), ('Requires-Python', project['requires-python'])]:
        require(value.get(field) == expected, field)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdist', required=True, type=Path)
    parser.add_argument('--wheels', required=True, nargs='+', type=Path)
    parser.add_argument('--build-logs', nargs='*', type=Path, default=[])
    args = parser.parse_args(argv)
    project = tomllib.loads(Path('pyproject.toml').read_text())['project']
    license_bytes = Path('LICENSE').read_bytes()
    assets = [Path('verinote/rules.dl')]
    for pattern in ('web/*', 'benchmark_data/v1/*.json', 'benchmark_data/repair-cases/*.json'):
        assets.extend(p for p in Path('verinote').glob(pattern) if p.is_file())
    with tarfile.open(args.sdist) as archive:
        members = archive.getmembers(); names_safe([m.name for m in members])
        require(all(m.isfile() or m.isdir() for m in members), 'sdist links/special files')
        infos = [m.name for m in members if len(PurePosixPath(m.name).parts) == 2 and m.name.endswith('/PKG-INFO')]
        require(len(infos) == 1, 'sdist PKG-INFO')
        root = str(PurePosixPath(infos[0]).parent)
        def read(name):
            stream = archive.extractfile(name)
            require(stream is not None, 'sdist file')
            assert stream is not None
            return stream.read()
        source_metadata = metadata(read(infos[0]), project)
        require(read(root+'/LICENSE') == license_bytes, 'sdist LICENSE bytes')
        for asset in assets: require(read(root+'/'+asset.as_posix()) == asset.read_bytes(), 'sdist asset '+str(asset))
    for path in args.wheels:
        with zipfile.ZipFile(path) as wheel:
            names = wheel.namelist(); names_safe(names)
            require(not any(stat.S_ISLNK(info.external_attr >> 16) for info in wheel.infolist()), 'wheel symlink')
            infos = [n for n in names if n.endswith('.dist-info/METADATA')]
            require(len(infos) == 1, 'wheel METADATA')
            base = str(PurePosixPath(infos[0]).parent)
            wheel_metadata = metadata(wheel.read(infos[0]), project)
            require(sorted(wheel_metadata.get_all('Requires-Dist', [])) == sorted(source_metadata.get_all('Requires-Dist', [])), 'runtime requirements')
            require(wheel.read(base+'/licenses/LICENSE') == license_bytes, 'wheel LICENSE bytes')
            entries = configparser.ConfigParser(); entries.read_string(wheel.read(base+'/entry_points.txt').decode())
            require(dict(entries['console_scripts']) == project['scripts'], 'console entry points')
            for asset in assets: require(wheel.read(asset.as_posix()) == asset.read_bytes(), 'wheel asset '+str(asset))
    for path in args.build_logs:
        require(not re.search(r'project\.license.*TOML table.*deprecated|License classifiers are deprecated', path.read_text()), 'deprecated licensing warning')
    print(f'License metadata, LICENSE bytes, {len(assets)} assets and {len(args.wheels)} wheels verified')


if __name__ == '__main__': main()
