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


class PortConflict(LifecycleError):
    """A host port the VM needs cannot be used.

    The advice is kept apart from the message on purpose. The CLI shows
    it as a command to run, the GUI offers a button that does the same
    thing, and neither has to parse the other's wording.
    """

    def __init__(self, message: str, *, vm: str = "", hint: str = "") -> None:
        super().__init__(message)
        self.vm = vm
        self.hint = hint


class AlreadyExists(VmhubError):
    def __init__(self, message: str, *, name: str = "") -> None:
        super().__init__(message)
        # The name that collided -- empty when the caller did not have a single
        # well-formed name to point at, so callers that don't care can ignore it.
        self.name = name


class NotFound(VmhubError):
    pass


class ProjectRootError(VmhubError):
    pass
