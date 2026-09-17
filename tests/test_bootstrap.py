"""Regression tests for the Windows bootstrap self-check.

These tests exercise the Python helper and verify script structure, not a real
powershell.exe process or WASAPI device. Native Windows acceptance is separate.
"""
import importlib.util
from importlib import metadata
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dependency_check", ROOT / "dependency_check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def req_file(tmp_path, text="example==1.2.3\n"):
    path = tmp_path / "requirements.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_installed_pins_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(check.metadata, "version", lambda name: "1.2.3")
    assert check.check_requirements(req_file(tmp_path)) == []


def test_wrong_version_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(check.metadata, "version", lambda name: "1.2.2")
    assert "required 1.2.3" in check.check_requirements(req_file(tmp_path))[0]


def test_missing_distribution_fails(monkeypatch, tmp_path):
    def missing(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(check.metadata, "version", missing)
    assert "not installed" in check.check_requirements(req_file(tmp_path))[0]


@pytest.mark.parametrize("platform,expected", [("linux", []), ("win32", ["PyAudioWPatch"])])
def test_platform_marker(monkeypatch, tmp_path, platform, expected):
    names = []
    monkeypatch.setattr(check.sys, "platform", platform)
    monkeypatch.setattr(check.metadata, "version", lambda name: names.append(name) or "0.2.12.8")
    text = "PyAudioWPatch==0.2.12.8; sys_platform == 'win32'\n"
    assert check.check_requirements(req_file(tmp_path, text)) == []
    assert names == expected


def test_actual_project_requirements(monkeypatch):
    versions = {"aiohttp": "3.14.3", "zeroconf": "0.151.3", "qrcode": "8.2",
                "imageio-ffmpeg": "0.6.0", "PyAudioWPatch": "0.2.12.8"}
    monkeypatch.setattr(check.sys, "platform", "win32")
    monkeypatch.setattr(check.metadata, "version", versions.__getitem__)
    assert check.check_requirements(ROOT / "requirements.txt") == []


@pytest.mark.parametrize("text", ["example>=1.2.3", "example==1.2.3; python_version >= '3.10'", "-r other.txt"])
def test_unknown_requirement_syntax_fails_closed(tmp_path, text):
    with pytest.raises(ValueError):
        check.check_requirements(req_file(tmp_path, text))


def test_bom_comments_and_spaces(monkeypatch, tmp_path):
    monkeypatch.setattr(check.metadata, "version", lambda name: "1.2.3")
    assert check.check_requirements(req_file(tmp_path, "\ufeff# Pinned\n\nexample == 1.2.3 # comment\n")) == []


def fake_components(monkeypatch, tmp_path, *, platform="linux", returncode=0):
    executable = tmp_path / "Folder with spaces Яндекс" / "ffmpeg.exe"
    executable.parent.mkdir()
    executable.touch()
    imports = []
    calls = []
    def load(name):
        imports.append(name)
        return SimpleNamespace(get_ffmpeg_exe=lambda: str(executable))
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=returncode)
    monkeypatch.setattr(check.importlib, "import_module", load)
    monkeypatch.setattr(check.sys, "platform", platform)
    monkeypatch.setattr(check.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(check.subprocess, "run", run)
    return executable, imports, calls


def test_component_check_preserves_path_no_shell(monkeypatch, tmp_path):
    executable, imports, calls = fake_components(monkeypatch, tmp_path)
    assert check.check_components() == str(executable)
    assert "pyaudiowpatch" not in imports
    args, kwargs = calls[0]
    assert args == [str(executable), "-version"]
    assert not kwargs.get("shell", False)
    assert kwargs["timeout"] == 15


def test_windows_audio_extension_is_imported_not_opened(monkeypatch, tmp_path):
    executable, imports, calls = fake_components(monkeypatch, tmp_path, platform="win32")
    assert check.check_components() == str(executable)
    assert "pyaudiowpatch" in imports
    assert calls[0][1]["creationflags"] == 0x08000000


def test_ffmpeg_failure_propagates(monkeypatch, tmp_path):
    fake_components(monkeypatch, tmp_path, returncode=5)
    with pytest.raises(RuntimeError, match="code 5"):
        check.check_components()


def test_missing_ffmpeg_fails(monkeypatch, tmp_path):
    executable, _, calls = fake_components(monkeypatch, tmp_path)
    executable.unlink()
    with pytest.raises(RuntimeError, match="missing"):
        check.check_components()
    assert calls == []


def test_ffmpeg_timeout_propagates(monkeypatch, tmp_path):
    fake_components(monkeypatch, tmp_path)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 15)
    monkeypatch.setattr(check.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        check.check_components()


def test_main_success(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(check, "check_requirements", lambda path: [])
    monkeypatch.setattr(check, "check_components", lambda: "C:/YA/ffmpeg.exe")
    assert check.main(["--requirements", str(req_file(tmp_path))]) == 0
    assert "Dependency self-check OK." in capsys.readouterr().out


def test_main_missing_package_skips_component_check(monkeypatch, capsys):
    monkeypatch.setattr(check, "check_requirements", lambda path: ["package: not installed"])
    def unexpected():
        raise AssertionError("Must not proceed on mismatched requirements")
    monkeypatch.setattr(check, "check_components", unexpected)
    assert check.main([]) == 1
    assert "not installed" in capsys.readouterr().out


def test_main_import_error_is_failure(monkeypatch, capsys):
    monkeypatch.setattr(check, "check_requirements", lambda path: [])
    def broken():
        raise ImportError("missing audio DLL")
    monkeypatch.setattr(check, "check_components", broken)
    assert check.main([]) == 1
    assert "missing audio DLL" in capsys.readouterr().out


def test_bootstrap_no_inline_python_and_checks_before_install():
    script = (ROOT / "bootstrap.ps1").read_text(encoding="utf-8-sig")
    assert "& $Python -c " not in script
    invocation = "Invoke-PrivatePython -Arguments @('-u',$CheckScript,'--requirements',$Requirements)"
    assert 'CreateNoWindow=$true' in script
    assert '$CheckScript' in script and '$CheckCode' in script
    assert script.index("$CheckCode =") < script.index("if ($NeedsInstall)")
    assert "--force-reinstall','--no-cache-dir'" in script



def test_real_ffmpeg_version_without_audio_capture():
    try:
        import imageio_ffmpeg
    except ImportError:
        pytest.skip("imageio-ffmpeg is not available in this test environment")
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run([exe, "-version"], capture_output=True, timeout=15)
    assert result.returncode == 0
    assert b"ffmpeg version" in result.stdout
