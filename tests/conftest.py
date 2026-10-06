"""Test isolation.

Every test runs against a throwaway project root. ``VMHUB_HOME`` is read
inside ``paths.project_root()`` on every call, and nothing in the codebase
caches it or imports it directly -- ``paths.project_root()`` is the only
access path -- so pointing the variable at a temp directory re-points every
path helper there is. There is no import that could have bypassed the
substitution and no cache that could have gone stale.

Two rules for tests in this suite:

1. Never call ``monkeypatch.undo()``. It undoes *every* monkeypatch, including
   the sandbox's, so the rest of the test resolves to the real project root.
   Use ``monkeypatch.context()`` to scope a patch you want removed early.
2. If a test needs ``VMHUB_HOME`` gone, chdir into the temp tree first.
   Discovery walks up from the cwd and can otherwise walk into the checkout.

The autouse guard checks before *and* after each test, because the failure it
catches is not a wrong answer, it is damage: these helpers resolve to the
real project root by default, so a test that reached one would act on whatever
is really there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vmhub import paths
from vmhub.errors import ProjectRootError


@pytest.fixture
def project_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root that satisfies the marker check, isolated from the real one."""
    root = tmp_path / "vmhub"
    (root / paths.BLUEPRINTS_DIR).mkdir(parents=True)
    (root / "vmctl").write_text("#!/bin/sh\n# test sandbox\n")
    monkeypatch.setenv(paths.ENV_HOME, str(root))
    monkeypatch.chdir(root)
    return root


@pytest.fixture(autouse=True)
def _sandbox_guard(project_home: Path):
    def assert_isolated() -> None:
        try:
            resolved = paths.project_root().resolve()
        except ProjectRootError:
            # A test may deliberately point VMHUB_HOME at something that is not
            # a project root -- refusing that is the behaviour under test, not
            # an escape from the sandbox.
            return
        expected = project_home.resolve()
        if resolved != expected:
            raise AssertionError(
                "test escaped its sandbox: project_root() resolved to "
                f"{resolved}, expected {expected}"
            )

    assert_isolated()
    yield
    assert_isolated()


@pytest.fixture
def make_vm(project_home: Path):
    """Create a VM directory the way the registry sees one."""

    def _make(
        name: str,
        *,
        meta: dict | None = None,
        spec_data: dict | None = None,
    ) -> Path:
        from vmhub import toml_io

        vm_dir = project_home / name
        vm_dir.mkdir(parents=True, exist_ok=True)
        if meta is not None:
            (vm_dir / paths.META_FILE).write_text(json.dumps(meta, indent=2))
        if spec_data is not None:
            (vm_dir / paths.SPEC_FILE).write_text(toml_io.dumps(spec_data))
        return vm_dir

    return _make


@pytest.fixture
def minimal_spec_data() -> dict:
    """A spec payload with the fields a VM needs to load without a blueprint."""
    return {
        "blueprint": "",
        "resources": {
            "cpus": 2,
            "ram": "4G",
            "disk": "64G",
            "disk_type": "scsi",
            "disk_fmt": "qcow2",
        },
        "network": {
            "mode": "user",
            "guest_ports": [22],
            "bind": "127.0.0.1",
        },
    }