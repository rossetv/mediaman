"""What can go wrong with a TV. Every message is fit to show to a person."""

from __future__ import annotations


class TvError(Exception):
    """Any failure to talk to a TV."""


class TvRefused(TvError):
    """The address, or where the TV's answer pointed, is not allowed."""


class TvUnreachable(TvError):
    """The TV did not answer, or went away."""


class TvLost(TvUnreachable):
    """The TV was sent a request, and may have acted on it, but the answer never came."""


class TvBadAnswer(TvError):
    """The TV answered with something that cannot be used."""


class TvFault(TvError):
    """The TV understood the request and said no."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code
