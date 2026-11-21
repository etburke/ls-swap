"""Errors raised by the OBD layer. The message is written for the person at the truck."""


class ObdError(Exception):
    pass


class ObdNoAdapter(ObdError):
    pass


class ObdTimeout(ObdError):
    pass


class ObdMonitorStuck(ObdTimeout):
    """The adapter gave no prompt after the stop byte: it may still be in monitor mode, so its settings can
    no longer be trusted (the connection must be set up again)."""

    def __init__(self, message: str = "The adapter did not leave monitor mode."):
        super().__init__(message)


class ObdCancelled(ObdError):
    """The person cancelled the read. Nothing is recorded."""

    def __init__(self, message: str = "Read cancelled; nothing was recorded."):
        super().__init__(message)


class ObdStopped(ObdError):
    """The person pressed Stop and the read has data worth keeping. Raised and caught inside the reader."""


class ObdProtocolError(ObdError):
    pass


class ObdVehicleSilent(ObdProtocolError):
    """The adapter is fine but the vehicle's first reply never came (key off, or the PCM is not answering)."""


class ObdAdapterError(ObdProtocolError):
    """The adapter itself reported a fault. `kind` is the adapter's own term. A `transient` error is
    worth one retry; `reset` means the adapter lost its settings (LV RESET) and must be re-initialised."""

    def __init__(self, message: str, kind: str = "", transient: bool = False, reset: bool = False):
        super().__init__(message)
        self.kind, self.transient, self.reset = kind, transient, reset


class ObdBusy(ObdAdapterError):
    """The PCM answered a PID request 'busy', 'conditions not correct' or 'response pending'. Transient,
    so the reader retries it once like a transient adapter error."""

    def __init__(self, nrc: int):
        super().__init__(f"The PCM was busy (negative response {nrc:02X}).",
                         kind=f"busy (NRC {nrc:02X})", transient=True)
        self.nrc = nrc


class ObdNotSupported(ObdProtocolError):
    """The PCM answered with a negative response: it does not support this service."""


class ObdForbiddenCommand(ObdError):
    """A command outside the read-only allowlist; it is never sent to the adapter."""
