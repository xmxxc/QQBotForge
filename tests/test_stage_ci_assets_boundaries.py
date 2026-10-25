"""CI 资产叠加脚本的类型冲突与工作区写入边界 (#86)

全部在临时目录中离线执行真实的 .github/scripts/stage-ci-assets.sh；
STAGE_CI_ASSETS_SCRIPT 可指向其他版本的脚本以对照旧行为。
"""

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('STAGE_CI_ASSETS_SCRIPT', ROOT / '.github/scripts/stage-ci-assets.sh'))
PINNED_ASSETS = [
    '04d901726cc6d94bae1b2b5524521f997a326e10',
    '380ac106188995dbb2541104686230a6a0cbafd8',
]

pytestmark = pytest.mark.skipif(shutil.which('bash') is None, reason='bash is required')


def stage(*args):
    return subprocess.run(['bash', str(SCRIPT), *map(str, args)], capture_output=True, text=True)


def write(path, content='x\n'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def snapshot(root):
    """{相对路径: (类型, 内容或链接目标)}，不跟随任何符号链接。"""
    entries = {}
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(current, name)
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries[rel] = ('symlink', os.readlink(path))
            elif path.is_dir():
                entries[rel] = ('directory', None)
            else:
                entries[rel] = ('file', path.read_bytes())
    return entries


@pytest.fixture
def layout(tmp_path):
    source = tmp_path / 'source'
    assets = tmp_path / 'assets'
    outside = tmp_path / 'outside'
    for path in (source, assets / 'tests', outside):
        path.mkdir(parents=True)
    write(outside / 'keep.txt', 'outside\n')
    return source, assets, outside


# ==================== 正常控制组 ====================


def test_same_content_file_is_accepted(layout):
    source, assets, _ = layout
    write(assets / 'tests/test_a.py', 'same\n')
    write(source / 'tests/test_a.py', 'same\n')

    result = stage(source, assets)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (source / 'tests/test_a.py').is_symlink()
    assert (source / 'tests/test_a.py').read_text() == 'same\n'


def test_source_without_tests_gets_asset_root(layout):
    source, assets, _ = layout
    write(assets / 'tests/core/test_a.py')

    result = stage(source, assets)

    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot(source / 'tests') == snapshot(assets / 'tests')


def test_existing_business_tests_are_kept(layout):
    source, assets, _ = layout
    write(assets / 'tests/test_asset.py', 'asset\n')
    write(source / 'tests/test_business.py', 'business\n')

    result = stage(source, assets)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (source / 'tests/test_business.py').read_text() == 'business\n'
    assert (source / 'tests/test_asset.py').read_text() == 'asset\n'


def test_existing_nested_directory_is_merged(layout):
    source, assets, _ = layout
    write(assets / 'tests/core/test_asset.py', 'asset\n')
    write(source / 'tests/core/test_business.py', 'business\n')

    result = stage(source, assets)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (source / 'tests/core/test_business.py').read_text() == 'business\n'
    assert (source / 'tests/core/test_asset.py').read_text() == 'asset\n'


def test_identical_symlinks_are_accepted(layout):
    source, assets, _ = layout
    write(assets / 'tests/data.txt')
    (assets / 'tests/link.txt').symlink_to('data.txt')
    write(source / 'tests/data.txt')
    (source / 'tests/link.txt').symlink_to('data.txt')

    result = stage(source, assets)

    assert result.returncode == 0, result.stdout + result.stderr
    assert os.readlink(source / 'tests/link.txt') == 'data.txt'


def test_tests_root_symlink_inside_workspace_is_accepted(layout):
    source, assets, _ = layout
    write(assets / 'tests/test_a.py')
    (source / 'suite').mkdir()
    (source / 'tests').symlink_to('suite')

    result = stage(source, assets)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (source / 'suite/test_a.py').is_file()


# ==================== 内容与类型冲突 ====================


def test_different_content_is_rejected(layout):
    source, assets, _ = layout
    write(assets / 'tests/test_a.py', 'asset\n')
    write(source / 'tests/test_a.py', 'business\n')

    result = stage(source, assets)

    assert result.returncode != 0
    assert 'would overwrite source test: tests/test_a.py' in result.stdout
    assert (source / 'tests/test_a.py').read_text() == 'business\n'


def test_asset_symlink_does_not_replace_same_content_source_file(layout):
    source, assets, _ = layout
    write(assets / 'tests/data.txt', 'same\n')
    (assets / 'tests/test_a.py').symlink_to('data.txt')
    write(source / 'tests/test_a.py', 'same\n')
    before = snapshot(source)

    result = stage(source, assets)

    assert result.returncode != 0
    assert 'type conflict at tests/test_a.py: source is file, asset is symlink' in result.stdout
    assert snapshot(source) == before


def test_asset_file_does_not_replace_same_content_source_symlink(layout):
    source, assets, outside = layout
    write(assets / 'tests/keep.txt', 'outside\n')
    (source / 'tests').mkdir()
    (source / 'tests/keep.txt').symlink_to(outside / 'keep.txt')
    before = snapshot(source), snapshot(outside)

    result = stage(source, assets)

    assert result.returncode != 0
    assert 'type conflict at tests/keep.txt: source is symlink, asset is file' in result.stdout
    assert (snapshot(source), snapshot(outside)) == before


def test_symlink_targets_differing_by_trailing_newline_conflict(layout):
    source, assets, _ = layout
    (assets / 'tests/link.txt').symlink_to('data.txt')
    (source / 'tests').mkdir()
    (source / 'tests/link.txt').symlink_to('data.txt\n')

    result = stage(source, assets)

    assert result.returncode != 0
    assert 'would overwrite source test: tests/link.txt' in result.stdout
    assert os.readlink(source / 'tests/link.txt') == 'data.txt\n'


def test_asset_directory_does_not_replace_source_file(layout):
    source, assets, _ = layout
    write(assets / 'tests/core/test_a.py')
    write(source / 'tests/core', 'business\n')

    result = stage(source, assets)

    assert result.returncode != 0
    assert 'type conflict at tests/core: source is file, asset is directory' in result.stdout
    assert 'tests/core/test_a.py' not in result.stdout
    assert (source / 'tests/core').read_text() == 'business\n'


# ==================== 工作区写入边界 ====================


def test_tests_root_symlink_outside_workspace_is_rejected(layout):
    source, assets, outside = layout
    write(assets / 'tests/test_a.py')
    (source / 'tests').symlink_to(outside)
    before = snapshot(outside)

    result = stage(source, assets)

    assert result.returncode != 0
    assert 'outside source workspace' in result.stdout
    assert str(outside) in result.stdout
    assert snapshot(outside) == before


def test_tests_root_relative_symlink_escaping_workspace_is_rejected(layout):
    source, assets, outside = layout
    write(assets / 'tests/test_a.py')
    (source / 'tests').symlink_to('../outside')
    before = snapshot(outside)

    result = stage(source, assets)

    assert result.returncode != 0
    assert 'outside source workspace' in result.stdout
    assert snapshot(outside) == before


def test_workspace_trailing_newline_does_not_widen_boundary(tmp_path):
    source = tmp_path / 'source\n'
    outside = tmp_path / 'source' / 'outside'
    assets = tmp_path / 'assets'
    source.mkdir()
    outside.mkdir(parents=True)
    write(assets / 'tests/new.txt')
    (source / 'tests').symlink_to(outside)

    result = stage(source, assets)

    assert result.returncode != 0
    assert 'outside source workspace' in result.stdout
    assert not (outside / 'new.txt').exists()


def test_dangling_tests_root_symlink_is_rejected(layout):
    source, assets, _ = layout
    write(assets / 'tests/test_a.py')
    (source / 'tests').symlink_to('missing')

    result = stage(source, assets)

    assert result.returncode != 0
    assert 'not an existing directory' in result.stdout
    assert not (source / 'missing').exists()


def test_nested_directory_symlink_outside_workspace_is_rejected(layout):
    source, assets, outside = layout
    write(assets / 'tests/core/test_a.py')
    (source / 'tests').mkdir()
    (source / 'tests/core').symlink_to(outside)
    before = snapshot(outside)

    result = stage(source, assets)

    assert result.returncode != 0
    assert 'type conflict at tests/core: source is symlink, asset is directory' in result.stdout
    assert snapshot(outside) == before


def test_dangling_source_symlink_at_asset_path_is_rejected(layout):
    source, assets, outside = layout
    write(assets / 'tests/test_a.py')
    (source / 'tests').mkdir()
    (source / 'tests/test_a.py').symlink_to(outside / 'created.py')

    result = stage(source, assets)

    assert result.returncode != 0
    assert 'type conflict at tests/test_a.py: source is symlink, asset is file' in result.stdout
    assert not (outside / 'created.py').exists()


# ==================== 参数校验 ====================


@pytest.mark.parametrize('args', [[], ['only-one'], ['a', 'b', 'c']])
def test_wrong_argument_count_prints_usage(args):
    result = stage(*args)

    assert result.returncode == 2
    assert 'usage: stage-ci-assets.sh' in result.stderr


@pytest.mark.parametrize('which', ['source', 'assets'])
def test_empty_argument_is_rejected(layout, which):
    source, assets, _ = layout
    write(assets / 'tests/test_a.py')

    result = stage('' if which == 'source' else source, '' if which == 'assets' else assets)

    assert result.returncode == 2
    assert 'must not be empty' in result.stderr
    assert not (source / 'tests').exists()


def test_missing_source_directory_is_rejected(layout, tmp_path):
    _, assets, _ = layout
    write(assets / 'tests/test_a.py')

    result = stage(tmp_path / 'absent', assets)

    assert result.returncode == 2
    assert 'source-dir is not a directory' in result.stderr
    assert not (tmp_path / 'absent').exists()


def test_missing_assets_are_rejected(layout, tmp_path):
    source, _, _ = layout

    result = stage(source, tmp_path / 'absent')

    assert result.returncode == 1
    assert 'test assets missing' in result.stdout
    assert not (source / 'tests').exists()


# ==================== 固定资产装配 ====================


def _git(*args, **kwargs):
    return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, **kwargs)


@pytest.mark.parametrize('source_ref', ['HEAD', 'origin/personal'])
@pytest.mark.parametrize('assets_sha', PINNED_ASSETS)
def test_pinned_assets_stage_onto_source(tmp_path, assets_sha, source_ref):
    if shutil.which('git') is None:
        pytest.skip('git is not available')
    for ref in (source_ref, assets_sha):
        if _git('cat-file', '-e', f'{ref}^{{commit}}').returncode:
            pytest.skip(f'{ref} is not available in this checkout')
    source = tmp_path / 'source'
    assets = tmp_path / 'assets'
    for ref, destination, paths in [(source_ref, source, ['tests']), (assets_sha, assets, ['tests'])]:
        destination.mkdir()
        archive = tmp_path / f'{destination.name}.tar'
        archive.write_bytes(_git('archive', ref, *paths, check=True).stdout)
        with tarfile.open(archive) as stream:
            stream.extractall(destination, filter='data')
    business = snapshot(source / 'tests')

    result = stage(source, assets)

    assert result.returncode == 0, result.stdout + result.stderr
    staged = snapshot(source / 'tests')
    assert staged == {**business, **snapshot(assets / 'tests')}
