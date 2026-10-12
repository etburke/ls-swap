"""Errors raised by the OBD layer. The message is written for the person at the truck."""


class ObdError(Exception):
    pass


class ObdNoAdapter(ObdError):
    pass


class ObdTimeout(ObdError):
    pass


class ObdProtocolError(ObdError):
    pass


class ObdNotSupported(ObdProtocolError):
    """The PCM answered with a negative response: it does not support this service."""
