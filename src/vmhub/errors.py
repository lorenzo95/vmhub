class VmhubError(Exception):
    pass


class SpecError(VmhubError):
    pass


class PodmanError(VmhubError):
    pass


class QmpError(VmhubError):
    pass


class QgaError(VmhubError):
    pass


class DiskError(VmhubError):
    pass


class LifecycleError(VmhubError):
    pass


class VmRunning(LifecycleError):
    pass


class VmNotRunning(LifecycleError):
    pass


class TemplateInUse(LifecycleError):
    pass


class NoFreePort(LifecycleError):
    pass


class AlreadyExists(VmhubError):
    pass


class NotFound(VmhubError):
    pass
