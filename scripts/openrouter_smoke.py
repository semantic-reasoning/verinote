"""Explicitly opted-in single synthetic request; never part of default CI."""
import argparse
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from verinote.config import ConfigError, load_config
from verinote.openrouter import OpenRouter
from verinote.transports import Credentials, ExecutionOptions


def main(argv=None):
    parser = argparse.ArgumentParser(description='One real OpenRouter request; may incur charges. No retries.')
    parser.add_argument('--allow-network', action='store_true', required=True,
                        help='explicitly authorize this single external request and possible charge')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--model', required=True)
    args = parser.parse_args(argv)
    try:
        credentials = Credentials.from_server(load_config(args.config))
        with tempfile.TemporaryDirectory(prefix='verinote-smoke-') as folder:
            result = OpenRouter().complete('openrouter', args.model,
                'Return exactly this JSON object and no other text: {"ok":true}', Path(folder), phase=None,
                options=ExecutionOptions(max_tokens=256, timeout=60), credentials=credentials, call_id=uuid4().hex)
        meta = result.envelope.safe_metadata
        valid = False
        if result.response is not None:
            try:
                value = json.loads(result.response)
                valid = isinstance(value, dict) and set(value) == {'ok'} and value['ok'] is True
            except (ValueError, RecursionError):
                pass
        # Adapter-attested allowlist only: never raw response, headers, errors or configuration.
        output = {key: meta.get(key) for key in ('http_status','transmission_state','resolved_model','finish_reason','usage')}
        output.update(transport_status=result.envelope.transport_status, error_code=result.error_code,
                      expected_json_received=valid, max_tokens=256, timeout_seconds=60)
        print(json.dumps(output, ensure_ascii=True, allow_nan=False))
        return 0 if valid and result.envelope.response_verified and result.error_code is None else 1
    except (ConfigError, ValueError, OSError):
        print('{"error":"configuration_or_execution_failed"}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
