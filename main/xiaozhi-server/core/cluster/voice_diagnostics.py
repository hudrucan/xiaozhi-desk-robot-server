"""Bounded lifecycle metadata, never raw logs, audio, transcripts or credentials."""
import copy
import re
from collections import deque
from datetime import datetime, timezone

PROTOCOL = 'xiaozhi-voice-diagnostics-v1'
MAX_EVENTS = 64
EVENTS = {'session_opened', 'session_closed', 'asr_started', 'asr_admitted', 'asr_restarting', 'asr_final',
          'llm_started', 'llm_first_chunk', 'llm_complete', 'tts_started',
          'tts_complete', 'turn_failed', 'turn_complete', 'turn_aborted'}
STATES = {'idle', 'asr', 'asr_restarting', 'llm', 'tts', 'done', 'error', 'aborted'}


class VoiceDiagnostics:
    def __init__(self):
        self.events = deque(maxlen=MAX_EVENTS)
        self.sequence = 0

    def record(self, event, session_id, **fields):
        if (not isinstance(event, str) or event not in EVENTS or not isinstance(session_id, str)
                or not re.fullmatch('[0-9a-f]{32}', session_id)):
            return
        safe = {}
        for key, value in fields.items():
            if key in {'elapsed_ms', 'frames', 'generation'} and type(value) is int and 0 <= value <= 10000000:
                safe[key] = value
            elif key == 'worker_id' and isinstance(value, str) and re.fullmatch('[A-Za-z0-9_-]{1,192}', value):
                safe[key] = value
            elif key == 'code' and isinstance(value, str) and re.fullmatch('(asr|llm|tts|worker_rpc|voice)_[a-z_]{1,48}', value):
                safe[key] = value
        self.sequence += 1
        self.events.append({'seq': self.sequence, 'at': datetime.now(timezone.utc).isoformat(),
                            'event': event, 'session_id': session_id, **safe})

    def snapshot(self):
        return copy.deepcopy(list(self.events))


def safe_snapshot(value, node):
    """Verify a configured peer's exact safe schema before returning it to UI."""
    if (not isinstance(value, dict) or set(value) != {'protocol', 'node_id', 'sessions', 'events', 'mcp'}
            or value['protocol'] != PROTOCOL or value['node_id'] != node or value['mcp'] is not False
            or not isinstance(value['sessions'], dict) or set(value['sessions']) != STATES
            or any(type(n) is not int or not 0 <= n <= 128 for n in value['sessions'].values())
            or not isinstance(value['events'], list) or len(value['events']) > MAX_EVENTS):
        raise ValueError('Invalid voice diagnostics')
    for entry in value['events']:
        if (not isinstance(entry, dict) or not {'seq', 'at', 'event', 'session_id'} <= set(entry)
                or set(entry) - {'seq', 'at', 'event', 'session_id', 'elapsed_ms', 'frames', 'generation', 'worker_id', 'code'}
                or type(entry['seq']) is not int or not 1 <= entry['seq'] <= 9007199254740991
                or not isinstance(entry['at'], str) or len(entry['at']) > 40):
            raise ValueError('Invalid voice diagnostic event')
        try:
            stamp = datetime.fromisoformat(entry['at'])
            if stamp.utcoffset() is None:
                raise ValueError
        except (ValueError, OverflowError):
            raise ValueError('Invalid voice diagnostic timestamp') from None
        checker = VoiceDiagnostics()
        fields = {k: v for k, v in entry.items() if k not in {'seq', 'at', 'event', 'session_id'}}
        checker.record(entry['event'], entry['session_id'], **fields)
        if not checker.events or any(checker.events[0].get(k) != v for k, v in fields.items()):
            raise ValueError('Invalid voice diagnostic fields')
    return copy.deepcopy(value)
