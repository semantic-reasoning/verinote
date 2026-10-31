"""Optional per-call domain verdicts through the sole HTTP recording writer."""
import json
from contextlib import contextmanager

from .usage import safe_file


class Verdict:
    failed = False

    def reject(self):
        self.failed = True


def notify(cli, folder, agent, success):
    if agent != 'openrouter':
        return
    try:
        raw = safe_file(folder, 'run.json')
        meta = json.loads(raw) if raw else None
        callback = getattr(cli, 'update_caller_validation', None)
        if isinstance(meta, dict) and meta.get('transport') == 'http' and callable(callback):
            callback(folder, meta.get('provider_call_id'), 'success' if success else 'failed')
    except Exception:
        pass  # Optional recording must preserve the primary domain outcome.


@contextmanager
def caller_validation(cli, folder, agent):
    """Enter only after complete returned; never notify an older failed call."""
    verdict = Verdict()
    try:
        yield verdict
    except Exception:
        notify(cli, folder, agent, False)
        raise
    else:
        notify(cli, folder, agent, not verdict.failed)
