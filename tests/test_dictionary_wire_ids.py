"""Literal wire IDs, historical decoding and unpaid pipeline regressions."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_workflow import approved_dictionary, extraction_response

from verinote.dictionary_context import (
    COLUMNS,
    PROTOCOL,
    admission_policy,
    chunk_payload,
    decode,
    encode,
    pinned_dictionary,
)
from verinote.ontology import (
    bounded_prompt,
    dictionary_context,
    dictionary_terms,
    extraction_prompts,
)
from verinote.service import Service
from verinote.store import digest


def historical_wire(definitions):
    strings=[]
    def ref(value):
        if isinstance(value,list): return [ref(v) for v in value]
        if value not in strings: strings.append(value)
        return strings.index(value)
    return {'dictionary_wire_version':1,'columns':copy.deepcopy(COLUMNS),'strings':strings,
            'rows':{kind:[[ref(row[c]) for c in columns] for row in definitions[kind]] for kind,columns in COLUMNS.items()},
            'sha256':digest(definitions)}


class LiteralCodec(unittest.TestCase):
    def definitions(self):
        return {'entities':[{'id':'0','name':'영','type':'T'}, {'id':'001','name':'한\\줄\n"이름"','type':'T'}],
                'aliases':[{'alias':'1174','entity':'001'}],
                'relations':[{'id':'depends_on','description':'조건·부정 보존','subject_types':['T'],'object_types':['T']}]}

    def test_literal_identifiers_and_historical_round_trip(self):
        definitions=self.definitions(); before=copy.deepcopy(definitions)
        wire=encode(definitions)
        self.assertEqual(wire['dictionary_wire_version'],2); self.assertNotIn('strings',wire)
        self.assertEqual(wire['rows']['entities'][0][0],'0')
        self.assertEqual(wire['rows']['entities'][1][0],'001')
        self.assertEqual(wire['rows']['aliases'][0],['1174','001'])
        self.assertEqual(decode(wire),definitions); self.assertEqual(definitions,before)
        old=historical_wire(definitions); old_before=copy.deepcopy(old)
        self.assertEqual(decode(old),definitions); self.assertEqual(old,old_before)
        self.assertEqual(dictionary_terms({'subject':'0','predicate':'depends_on','object':'1174'},definitions),
                         {'subject':'0','predicate':'depends_on','object':'001'})
        with self.assertRaises(ValueError): dictionary_terms({'subject':'0','predicate':'1174','object':'001'},definitions)

    def test_strict_cells_arrays_counts_and_hash_for_both_versions(self):
        for wire in (encode(self.definitions()),historical_wire(self.definitions())):
            mutations=[lambda w:w.update(dictionary_wire_version=True),lambda w:w.update(extra=1),
                       lambda w:w.update(sha256='bad'),lambda w:w['columns']['entities'].reverse(),
                       lambda w:w['rows']['entities'][0].append('extra'),lambda w:w['rows']['entities'][0].__setitem__(0,True),
                       lambda w:w['rows']['entities'].append(w['rows']['entities'][0]),
                       lambda w:w['rows']['relations'][0].__setitem__(2,[]),lambda w:w['rows']['relations'][0].__setitem__(3,[False])]
            for mutate in mutations:
                value=copy.deepcopy(wire); mutate(value)
                with self.subTest(version=wire['dictionary_wire_version']),self.assertRaises(ValueError): decode(value)
        for cell in (0,1174,False,None,['0']):
            wire=encode(self.definitions());wire['rows']['entities'][0][0]=cell
            with self.assertRaises(ValueError): decode(wire)
        wire=encode(self.definitions());wire['strings']=[]
        with self.assertRaises(ValueError): decode(wire)
        old=historical_wire(self.definitions());old['rows']['entities'][0][0]='0'
        with self.assertRaises(ValueError): decode(old)
        with patch.dict('verinote.ontology.DICTIONARY_TOTAL_LIMITS',entities=1),self.assertRaises(ValueError): decode(historical_wire(self.definitions()))
        with patch('verinote.dictionary_context.MAX_DECODE_BYTES',10),self.assertRaises(ValueError): decode(encode(self.definitions()))

    def test_admission_policy_invalidates_old_protocol(self):
        current=admission_policy()
        with patch('verinote.dictionary_context.PROTOCOL','Dictionary wire v1 historical instructions\n'):
            self.assertNotEqual(admission_policy(),current)


class LiteralService(unittest.TestCase):
    setUp=workflow.Workflow.setUp
    tearDown=workflow.Workflow.tearDown

    def test_large_dictionary_initial_repair_and_recovery_send_literal_ids(self):
        source='S0000 requires S0001.\n'+' '.join(f'S{i:04}' for i in range(300))
        dictionary={'entities':[{'id':('0' if i==0 else '001' if i==1 else f'E{i:04}'+'i'*90),
                                'name':f'S{i:04}','type':'T'+'t'*40,'quote':f'S{i:04}'} for i in range(300)],
                    'aliases':[], 'relations':[{'id':'depends_on','description':'주어가 목적어에 직접 의존한다.',
                        'subject_types':['T'+'t'*40],'object_types':['T'+'t'*40],'quote':'S0000 requires S0001.'}]}
        fact={'subject':'0','predicate':'depends_on','object':'001','quote':'S0000 requires S0001.'}
        page={'title':'fixture','claims':[{'heading':'dependency','text':'S0000 requires S0001.','quote':'S0000 requires S0001.'}]}
        self.store.save_settings({'selected':'copilot','models':{},'dictionary_context_bytes':100})
        for mode in ('initial','repair','recovery','retry'):
            with self.subTest(mode=mode):
                doc=self.store.register(mode,mode+'.txt',source.encode(),source)
                item=approved_dictionary(self.store,doc['id'],dictionary)
                output=extraction_response([fact],dictionary,page)
                if mode=='repair': output['facts'][0]['quote']='absent'
                if mode=='recovery': output=extraction_response([],dictionary,page)
                outputs=[output]
                if mode=='retry':
                    bad=copy.deepcopy(output);bad['facts'][0]['predicate']='1174';bad['coverage'][0]['predicate']='1174'
                    outputs=[bad,output]
                if mode=='repair': outputs.append({'repairs':[{'path':'facts.0.quote','quote':fact['quote']}]})
                if mode=='recovery': outputs.append({'facts':[fact]})
                pin={'id':item['id'],'revision':item['revision'],'sha256':digest(item['payload']), 'payload':item['payload'],'extraction_contract':3}
                payload=chunk_payload(doc,0,source)|pinned_dictionary(pin)
                report={}; bounded_prompt(extraction_prompts(3)[0],payload,context_limit=100,diagnostics=report)
                self.assertLess(report['prompt_bytes'],report['raw_prompt_bytes'])
                # Initial request must fit only the compact form. Leave room for repair/recovery framing.
                limit=report['raw_prompt_bytes']-1
                calls=[]; pending=iter(outputs); outer=self
                class Fake:
                    prompt_byte_limit=staticmethod(lambda agent, limit=limit:limit)
                    def complete(self,agent,model,prompt,folder,outer=outer,calls=calls,pending=pending):
                        body=json.loads(prompt.split('입력(JSON):\n',1)[1]);wire=body['approved_dictionary']['payload']
                        outer.assertTrue(prompt.startswith(PROTOCOL));outer.assertEqual(wire['dictionary_wire_version'],2)
                        outer.assertNotIn('strings',wire);outer.assertEqual(decode(wire),dictionary_context(dictionary))
                        outer.assertEqual(wire['rows']['entities'][0][0],'0');outer.assertEqual(wire['rows']['entities'][1][0],'001')
                        outer.assertEqual(wire['rows']['relations'][0][0],'depends_on')
                        calls.append(Path(folder).name);return copy.deepcopy(next(pending))
                service=Service(self.store,self.root,Fake())
                run=service.start(doc['id'],'copilot','model',expected_version=1,expected_generation=1);service.close()
                saved=self.store.run(run['id'])
                if mode=='retry':
                    self.assertEqual(saved['status'],'error');self.assertIn('승인 사전에 없는 관계',saved['detail'])
                    service=Service(self.store,self.root,Fake())
                    run=service.start(doc['id'],'copilot','model',expected_version=1,expected_generation=1);service.close()
                    saved=self.store.run(run['id'])
                self.assertEqual(saved['status'],'success',saved['detail'])
                self.assertEqual(calls,['1'] if mode=='initial' else ['1','quote-repair' if mode=='repair' else '1' if mode=='retry' else 'recovery'])
                facts=[i['payload'] for i in self.store.document(doc['id'])['items'] if i['kind']=='fact']
                self.assertEqual(facts,[fact]);self.assertEqual(self.store.snapshot()['facts'],[])
