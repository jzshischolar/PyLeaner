"""Recovery-related exceptions for PyLeaner."""


class ServiceUnavailable(Exception):
    """Internal: the Lean server is restarting. Retryable unless the carrying
    task is flagged toxic.

    Raised into a worker when a poison sentinel aborts a blocked ``_didchange``/
    ``_didopen``. ``LspClient.submit_resilient`` treats it as "wait for
    ``server_ready`` and retry" — unless the result also carries ``toxic=True``,
    in which case it is converted to :class:`ToxicTaskError`.
    """


class ToxicTaskError(Exception):
    """Public: this task caused the server to crash or wedge, so it was rejected
    (not retried). The application should drop or regenerate the input —
    retrying it would re-crash the server.

    Attributes:
        task_type: The PyLeaner task type that was rejected.
        reason: Why it was rejected (e.g. the fatal stderr line, or "task
            exceeded the wedge deadline").
        input_text: The full ``text`` argument of the rejected task (empty if the
            task had no ``text``).
    """

    def __init__(self, task_type: str, reason: str, input_text: str):
        self.task_type = task_type
        self.reason = reason
        self.input_text = input_text
        super().__init__(f"{task_type!r} rejected by server ({reason})")
