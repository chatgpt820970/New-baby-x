"""Errors the poster can recover from versus errors that must stop the run."""


class ItemError(Exception):
    """This queue item cannot be posted. Move it to failed/ and try the next one."""

    def __init__(
        self,
        message: str,
        stage: str,
        *,
        stop_run: bool = False,
        buffer_post_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.stop_run = stop_run
        self.buffer_post_id = buffer_post_id


class RunAbort(Exception):
    """The run cannot continue. Leave the current item in place so a later run can retry it."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class TransientError(Exception):
    """A Buffer or network call failed in a way that a short wait might fix."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
