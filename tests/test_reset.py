"""Reset: the guest reboots, the container and its ports stay up.

The workflow this exists for is a hung guest: force-stop + start works
but tears down the container and reallocates nothing the user wanted
touched. Reset must be a QMP system_reset and nothing more -- and it
must refuse a VM that is not running, instead of pretending.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from vmhub import cli, lifecycle, paths, podman, qmp
from vmhub.cli import vms
from vmhub.errors import LifecycleError, QmpError


class _FakeQmp:
    instances: list["_FakeQmp"] = []
    fail_with: Exception | None = None

    def __init__(self, path: Path, *, timeout: float = 15.0) -> None:
        self.path = Path(path)
        self.commands: list[str] = []
        _FakeQmp.instances.append(self)

    def __enter__(self) -> "_FakeQmp":
        if _FakeQmp.fail_with is not None:
            raise _FakeQmp.fail_with
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def system_reset(self) -> None:
        self.commands.append("system_reset")


@pytest.fixture
def fake_qmp(monkeypatch: pytest.MonkeyPatch):
    _FakeQmp.instances = []
    _FakeQmp.fail_with = None
    monkeypatch.setattr(qmp, "Qmp", _FakeQmp)
    return _FakeQmp


def _running(monkeypatch: pytest.MonkeyPatch, running: bool = True) -> None:
    monkeypatch.setattr(podman, "exists", lambda vm: True)
    monkeypatch.setattr(podman, "is_running", lambda vm: running)


def test_reset_sends_system_reset_and_nothing_else(
    project_home, fake_qmp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _running(monkeypatch)
    lifecycle.reset("web-01")
    assert len(fake_qmp.instances) == 1
    client = fake_qmp.instances[0]
    assert client.commands == ["system_reset"]
    assert client.path == paths.qmp_path("web-01")


def test_reset_refuses_a_vm_that_is_not_running(
    project_home, fake_qmp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _running(monkeypatch, running=False)
    with pytest.raises(LifecycleError, match="not running"):
        lifecycle.reset("web-01")
    assert fake_qmp.instances == []


def test_reset_refuses_a_vm_with_no_container(
    project_home, fake_qmp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(podman, "exists", lambda vm: False)
    with pytest.raises(LifecycleError, match="not running"):
        lifecycle.reset("web-01")
    assert fake_qmp.instances == []


def test_reset_reports_a_qmp_failure_as_a_lifecycle_error(
    project_home, fake_qmp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _running(monkeypatch)
    fake_qmp.fail_with = QmpError("socket went away")
    with pytest.raises(LifecycleError, match="reset failed"):
        lifecycle.reset("web-01")


def test_cli_reset_reports_success_and_failure(
    project_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def ok(name, **kw):
        calls.append(name)

    monkeypatch.setattr(vms.lifecycle, "reset", ok)
    assert cli.cmd_reset(SimpleNamespace(name="web-01", quiet=True), cli.Out()) == 0
    assert calls == ["web-01"]

    def bad(name, **kw):
        raise LifecycleError("web-01 is not running")

    monkeypatch.setattr(vms.lifecycle, "reset", bad)
    assert cli.cmd_reset(SimpleNamespace(name="web-01", quiet=True), cli.Out()) == 1
