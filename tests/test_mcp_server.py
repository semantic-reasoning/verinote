import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_workflow import DICTIONARY, SOURCE, extraction_response

from verinote.store import Store

HAS_MCP = importlib.util.find_spec('mcp') is not None


@unittest.skipUnless(HAS_MCP, 'optional MCP extra not installed')
class StdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_stdio_full_workflow_and_errors(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'verinote.sqlite3')
            doc = store.register('문서', 'doc.txt', SOURCE.encode(), SOURCE)
            # A live ordinary run must not be interrupted when MCP starts.
            other = store.register('other', 'other.txt', b'other', 'other')
            ordinary = store.create_run(other['id'], 'claude', '', 'ontology', 1, 1)
            params = StdioServerParameters(command=sys.executable,
                args=['-m', 'verinote.mcp_server', '--data-dir', tmp], env={'PATH': '', 'PYTHONPATH': str(Path(__file__).resolve().parents[1])})
            async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                initialized = await client.initialize()
                self.assertEqual(initialized.serverInfo.name, 'Verinote')
                names = {t.name for t in (await client.list_tools()).tools}
                self.assertEqual(names, {'list_documents', 'list_analysis_runs', 'start_analysis', 'get_run_status', 'get_analysis_task',
                                        'submit_dictionary_chunk', 'submit_extraction_chunk', 'finalize_run', 'cancel_run'})
                self.assertEqual(store.run(ordinary['id'])['status'], 'queued')

                async def invoke(name, args):
                    result = await client.call_tool(name, args)
                    self.assertFalse(result.isError, result)
                    return json.loads(result.content[0].text)

                documents = await invoke('list_documents', {})
                self.assertEqual(len(documents['documents']), 2)
                start = {'document_id': doc['id'], 'stage': 'ontology', 'expected_version': 1, 'expected_generation': 1}
                invalid = await client.call_tool('start_analysis', start | {'expected_version': True})
                self.assertTrue(invalid.isError)
                key = (await invoke('start_analysis', start))['id']
                task = await invoke('get_analysis_task', {'run_id': key})
                self.assertIn(SOURCE, task['prompt'])
                invalid = await client.call_tool('submit_dictionary_chunk', {'run_id': key, 'chunk_index': 1, 'output': {}})
                self.assertTrue(invalid.isError)
                self.assertEqual((await invoke('get_run_status', {'run_id': key}))['accepted_chunks'], 0)
                await invoke('submit_dictionary_chunk', {'run_id': key, 'chunk_index': 1, 'output': DICTIONARY})
                completed = await invoke('finalize_run', {'run_id': key})
                self.assertEqual(completed['status'], 'success')
                item = store.document(doc['id'])['items'][0]
                self.assertEqual(item['status'], 'pending')
                refused = await client.call_tool('start_analysis', start | {'stage': 'extract'})
                self.assertTrue(refused.isError)
                store.review(item['id'], 1, 'approved', 'human', 'review')
                key = (await invoke('start_analysis', start | {'stage': 'extract'}))['id']
                await invoke('submit_extraction_chunk', {'run_id': key, 'chunk_index': 1, 'output': extraction_response()})
                first = await invoke('finalize_run', {'run_id': key})
                self.assertEqual(first, await invoke('finalize_run', {'run_id': key}))
                self.assertEqual(len(first['candidate_ids']), 3)
                self.assertTrue(all(i['status'] == 'pending' for i in store.document(doc['id'])['items'] if i['kind'] != 'ontology'))


class StartupTests(unittest.TestCase):
    def test_help_and_invalid_paths_do_not_write_protocol_stdout(self):
        env = os.environ | {'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
        help_run = subprocess.run([sys.executable, '-m', 'verinote.mcp_server', '--help'], env=env, capture_output=True, timeout=20)
        self.assertEqual(help_run.returncode, 0)
        for path in ('relative', '/nonexistent/verinote-mcp-test-db'):
            result = subprocess.run([sys.executable, '-m', 'verinote.mcp_server', '--data-dir', path], env=env, capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b'')
