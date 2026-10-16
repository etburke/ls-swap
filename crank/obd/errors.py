"""Errors raised by the OBD layer. The message is written for the person at the truck."""


class ObdError(Exception):
    pass


class ObdNoAdapter(ObdError):
    pass


class ObdTimeout(ObdError):
    pass


class ObdProtocolError(ObdError):
    pass


class ObdAdapterError(ObdProtocolError):
    """The adapter itself reported a fault. `kind` is the adapter's own term. A `transient` error is
    worth one retry; `reset` means the adapter lost its settings (LV RESET) and must be re-initialised."""

    def __init__(self, message: str, kind: str = "", transient: bool = False, reset: bool = False):
        super().__init__(message)
        self.kind, self.transient, self.reset = kind, transient, reset


class ObdNotSupported(ObdProtocolError):
    """The PCM answered with a negative response: it does not support this service."""


class ObdForbiddenCommand(ObdError):
    """A command outside the read-only allowlist; it is never sent to the adapter."""
