"""Sample-scoped, durable timeout accounting at model tool boundaries."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
from .errors import LeanTaskTimeout, LeanTimeoutExhausted

_sample = ContextVar('lean_sample_timeout_policy', default=None)
_tool = ContextVar('lean_tool_submission_marker', default=None)


@contextmanager
def sample_timeout_scope(path, *, limit=3):
    if limit < 1:
        raise ValueError('Lean consecutive timeout limit must be positive')
    path = Path(path)
    state = json.loads(path.read_text()) if path.exists() else {'consecutive': 0, 'timeouts': {}}
    state['limit'] = limit
    token = _sample.set((path, state))
    try:
        if state['consecutive'] >= limit:
            raise LeanTimeoutExhausted(f"{state['consecutive']} consecutive Lean tool timeouts")
        yield
    finally:
        _sample.reset(token)


def note_lean_submission():
    marker = _tool.get()
    if marker is not None:
        marker['submitted'] = True


def _persist(policy):
    path, state = policy
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(state, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def run_lean_tool(callback, *, key=None):
    """Return (value, timeout feedback); unrelated errors pass through unchanged.

    One sample owns its journal under the stage claim lease. Replay of a timed
    out generation/tool id returns its original receipt without recounting it.
    """
    policy = _sample.get()
    state = policy[1] if policy else {'consecutive': 0, 'limit': 3, 'timeouts': {}}
    if key and key in state['timeouts']:
        return None, dict(state['timeouts'][key])
    marker = {'submitted': False}
    token = _tool.set(marker)
    try:
        try:
            result = callback()
        except LeanTaskTimeout as exc:
            state['consecutive'] += 1
            feedback = {
                'status': 'lean_timeout', 'success': False,
                'error': str(exc), 'consecutive_timeouts': state['consecutive'],
                'timeout_limit': state['limit'],
                'timeout_exhausted': state['consecutive'] >= state['limit'],
                'instruction': 'Lean timed out. Simplify the proof or reduce case enumeration before retrying.',
            }
            if key:
                state['timeouts'][key] = feedback
            if policy:
                _persist(policy)
            return None, feedback
        if marker['submitted'] and state['consecutive']:
            state['consecutive'] = 0
            if policy:
                _persist(policy)
        return result, None
    finally:
        _tool.reset(token)


def finish_timeout_feedback(feedback):
    """Call after recording the model-visible tool result, before any pause."""
    if feedback and feedback.get('timeout_exhausted'):
        raise LeanTimeoutExhausted(
            f"{feedback['consecutive_timeouts']} consecutive Lean tool timeouts")
