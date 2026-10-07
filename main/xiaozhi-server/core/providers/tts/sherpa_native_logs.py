"""Filter known native phoneme log spam while retaining real diagnostics."""
import os
import re
import tempfile
import threading
from collections import Counter

_NATIVE_STDERR_LOCK = threading.Lock()
_UNKNOWN_PHONEME_LOG = re.compile(
    rb".*piper-phonemize-lexicon\.cc:PiperPhonemesToIdsVits:\d+ "
    rb"Skip unknown phonemes\. Unicode codepoint: \\U\+([0-9A-Fa-f]+)\."
)


def generate_without_native_log_spam(generate, debug_log=None):
    """Run Sherpa while retaining every native error except known log spam."""
    with _NATIVE_STDERR_LOCK:
        original_stderr = os.dup(2)
        skipped = Counter()
        with tempfile.TemporaryFile() as captured:
            try:
                os.dup2(captured.fileno(), 2)
                result = generate()
            finally:
                os.dup2(original_stderr, 2)
                captured.seek(0)
                retained = []
                for line in captured.readlines():
                    match = _UNKNOWN_PHONEME_LOG.fullmatch(line.strip())
                    if match:
                        skipped[match.group(1).decode("ascii").upper()] += 1
                    elif line.strip():
                        retained.append(line)
                if retained:
                    os.write(original_stderr, b"".join(retained))
                os.close(original_stderr)

        if skipped:
            summary = ", ".join(
                f"U+{codepoint} x{count}"
                for codepoint, count in sorted(skipped.items())
            )
            if debug_log:
                debug_log(f"Sherpa skipped unsupported phonemes: {summary}")
        return result
