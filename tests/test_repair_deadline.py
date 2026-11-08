"""Synthetic calls only: the chunk deadline passing after a repair request is prepared and before the repair call."""
import json
import threading
import time
import unittest
from unittest.mock import patch

from test_failed_run_detail import Base, from_chunk
from test_quote_repair import broken
from test_workflow import extraction_response

from verinote import service


class RepairDeadline(Base):
    def drive(self, capacity):
        """Chunk 2's first answer needs a repair; the monotonic clock jumps past the deadline once its request is prepared."""
        offset, real_clock, real_prepare = [0.0], time.monotonic, service.prepare_dictionary_prompt
        third_called = threading.Event()
        def prepare(cli, agent, prompt, payload, limit, folder):
            prepared = real_prepare(cli, agent, prompt, payload, limit, folder)
            if folder.name == 'quote-repair':
                if capacity == 2: self.assertTrue(third_called.wait(5))  # chunk 3's call is already sent when the clock jumps
                offset[0] += service.CHUNK_TIMEOUT + 1
            return prepared
        def respond(index, repairing):
            if index == 3: third_called.set()
            return broken() if index == 2 else extraction_response()
        doc = self.document()
        with patch('verinote.service.prepare_dictionary_prompt', prepare), patch('time.monotonic', lambda: real_clock() + offset[0]):
            return self.sequential(doc, respond) if capacity is None else self.parallel(doc, respond, capacity)

    def test_uncalled_repair_is_not_counted_and_every_path_words_it_as_a_repair_timeout(self):
        for capacity in (None, 1, 2):
            with self.subTest(capacity=capacity):
                self.tearDown(); self.setUp()
                run, calls = self.drive(capacity)
                self.assertEqual(calls.count(2), 1)  # chunk 2's first call only: the repair was never sent
                self.assertEqual(run['status'], 'error')
                self.assertEqual(run['detail'], f'청크 2 · 관계 1 인용: 인용 보정 실패 (청크당 1회). 청크 실행 시간 제한 초과 ({service.CHUNK_TIMEOUT}초)'
                                                f' · 검증 완료 1/3청크 · 새 호출 {len(calls)}회 · {from_chunk(2)}')
                value = json.loads((self.root / 'runs' / run['id'] / 'extraction-timing.json').read_text())
                self.assertEqual(value['error_code'], 'timeout')


if __name__ == '__main__':
    unittest.main()
