import os
import pathlib
import shutil
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _write_executable(path, content):
    path.write_text(content)
    path.chmod(0o755)


def _run_installer(tmp_path, *, rust_version="1.97.1", rust_std_version="1.97.1",
                   inherited_api="29", python_api="24", crypto_import_exit="0"):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "install.sh", source / "install.sh")
    (source / "server.py").write_text("# installer fixture\n")
    (source / "requirements.txt").write_text("# installer fixture\n")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    pkg_log = tmp_path / "pkg.log"
    python_log = tmp_path / "python.log"

    _write_executable(fake_bin / "pkg", """#!/bin/sh
printf '%s\n' "$*" > "$FAKE_PKG_LOG"
""")
    _write_executable(fake_bin / "dpkg", """#!/bin/sh
[ "$1" = "--print-architecture" ] || exit 2
printf '%s\n' "${FAKE_ARCH:-aarch64}"
""")
    _write_executable(fake_bin / "dpkg-query", """#!/bin/sh
for package do :; done
case "$package" in
    rust) printf '%s' "$FAKE_RUST_VERSION" ;;
    rust-std-*) printf '%s' "$FAKE_RUST_STD_VERSION" ;;
    *) exit 1 ;;
esac
""")
    _write_executable(fake_bin / "python", """#!/bin/sh
if [ "${1:-}" = "-" ]; then
    payload=$(cat)
    printf '%s\n---\n' "$payload" >> "$FAKE_PYTHON_LOG"
    case "$payload" in
        *sysconfig*) printf '%s\n' "$FAKE_PYTHON_API" ;;
        *'import cffi'*) exit "${FAKE_CRYPTO_IMPORT_EXIT:-0}" ;;
        *) exit 2 ;;
    esac
    exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
    printf 'pip %s\n' "$*" >> "$FAKE_PYTHON_LOG"
    exit 0
fi
exit 2
""")

    env = os.environ.copy()
    env.update({
        "ANDROID_API_LEVEL": inherited_api,
        "FAKE_PKG_LOG": str(pkg_log),
        "FAKE_PYTHON_LOG": str(python_log),
        "FAKE_PYTHON_API": python_api,
        "FAKE_CRYPTO_IMPORT_EXIT": crypto_import_exit,
        "FAKE_RUST_VERSION": rust_version,
        "FAKE_RUST_STD_VERSION": rust_std_version,
        "HOME": str(tmp_path / "home"),
        "MCP_DEST": str(tmp_path / "dest"),
        "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
    })
    result = subprocess.run(
        ["sh", "install.sh"], cwd=source, env=env, text=True,
        capture_output=True)
    return result, pkg_log, python_log


def test_installer_prepares_native_dependencies_and_uses_python_api(tmp_path):
    result, pkg_log, python_log = _run_installer(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    pkg_args = pkg_log.read_text().split()
    assert "rust" in pkg_args
    assert "rust-std-aarch64-linux-android" in pkg_args
    assert "python-cryptography" in pkg_args
    assert "-y" in pkg_args
    assert "Ignoring inherited ANDROID_API_LEVEL=29" in result.stdout
    assert "Using ANDROID_API_LEVEL=24 from the active Python platform" in result.stdout

    python_calls = python_log.read_text()
    assert "import cffi" in python_calls
    assert "import cryptography" in python_calls
    assert "sysconfig.get_platform()" in python_calls
    assert 'get_config_var("ANDROID_API_LEVEL")' in python_calls
    assert "from packaging.tags import sys_tags" in python_calls
    assert "tag.platform == wheel_platform" in python_calls


def test_installer_fails_early_on_mismatched_rust_std(tmp_path):
    result, _, python_log = _run_installer(
        tmp_path, rust_version="1.97.1", rust_std_version="1.96.1")

    assert result.returncode != 0
    assert "Rust package mismatch" in result.stderr
    assert "rust=1.97.1" in result.stderr
    assert "rust-std-aarch64-linux-android=1.96.1" in result.stderr
    assert not python_log.exists()


def test_installer_fails_before_dependency_build_when_crypto_imports_fail(tmp_path):
    result, _, python_log = _run_installer(tmp_path, crypto_import_exit="1")

    assert result.returncode != 0
    assert "did not provide importable cryptography and cffi" in result.stderr
    assert "import cffi" in python_log.read_text()
    assert "pip install" not in python_log.read_text()
