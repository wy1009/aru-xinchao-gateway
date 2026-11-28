import asyncio
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from serein_library import store, search


@unittest.skipUnless(importlib.util.find_spec("serein") and importlib.util.find_spec("mcp"), "requires the Serein runtime")
class Materials(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / 'serein.db'
        self.db.touch()
        store.setup(self.db)
        self.settings = SimpleNamespace(database=self.db)

    def test_mcp_read_update_and_conflicting_revision(self):
        from mcp.server.fastmcp import FastMCP
        from serein_library.api import register
        server = FastMCP('synthetic')
        register(server,self.settings)
        async def scenario():
            async def call(name, **data):
                result = await server.call_tool(name,data)
                return result[1]
            for kind in store.CATEGORIES:
                body = '  第一段。\n\n' + ('保留这行，逐字读回。\n' * 900) + '\n旧约定\n'
                created = await call('create_material',kind=kind,title='示例',content=body,request_id=kind)
                again = await call('create_material',kind=kind,title='示例',content=body,request_id=kind)
                self.assertEqual(created,again)
                item = await call('read_material',id=created['id'])
                self.assertEqual(body,item['content'])
                saved = await call('update_material',id=item['id'],expected_revision=item['revision'],
                                   old_text='旧约定',new_text='新约定')
                self.assertEqual(saved['revision'],2)
                with self.assertRaisesRegex(Exception,'重读'):
                    await call('update_material',id=item['id'],expected_revision=1,content='并发覆盖')
                actual = await call('read_material',id=item['id'])
                self.assertEqual(actual['content'],body.replace('旧约定','新约定'))
                self.assertEqual(actual['title'],item['title'])
            self.assertEqual(len((await call('list_materials'))['items']),2)
        asyncio.run(scenario())

    def material_server(self):
        from mcp.server.fastmcp import FastMCP
        from serein_library.api import register
        server = FastMCP('test materials')
        register(server, self.settings)
        return server

    def test_aru_card_workflow_rejects_stale_and_misspelled_edits(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        server = self.material_server()

        def call(name, **data):
            return asyncio.run(server.call_tool(name, data))[1]

        catalog = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        self.assertEqual(set(catalog), {'list_materials', 'search_library', 'openMemorySource',
                                       'writeInformationCard', 'writeLongTermReference', 'updateLongTermReference'})
        self.assertFalse(catalog['writeInformationCard'].inputSchema['additionalProperties'])
        card = call('writeInformationCard', subject='小花园', content='薄荷')
        self.assertEqual(call('writeInformationCard', subject='小花园', content='薄荷'), card)
        barrier = Barrier(2)

        def edit(herb):
            original = call('openMemorySource', **card)
            barrier.wait(timeout=10)
            body = original['content'] + '、' + herb
            try:
                call('writeInformationCard', subject=original['title'], content=body, cardId=original['cardId'])
            except Exception as exc:
                if '重读' not in str(exc):
                    raise
                return None
            return body

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, ['紫苏', '迷迭香']))
        self.assertEqual(results.count(None), 1)
        latest = call('openMemorySource', **card)
        self.assertIn(latest['content'], results)
        with self.assertRaises(Exception):
            call('writeInformationCard', subject='新标题', content='误拼标识', cardID=latest['cardId'])
        with self.assertRaisesRegex(Exception, '先用'):
            call('writeInformationCard', subject='新标题', content='没有先读', cardId=card['id'])
        with self.assertRaisesRegex(Exception, '先用'):
            call('updateLongTermReference', content='误用标识', reference=latest['cardId'])
        self.assertEqual(len(call('list_materials')['items']), 1)

    def test_reference_summary_paging_import_alias_and_external_revision(self):
        from serein_library.aru_tools import PAGE_CHARS
        server = self.material_server()

        def call(name, **data):
            return asyncio.run(server.call_tool(name, data))[1]

        body = '甲' * PAGE_CHARS + '第二页'
        ref = call('writeLongTermReference', title='随笔', content=body, summary='原摘要')
        partial = call('openMemorySource', **ref, page=2, query='第二页')
        self.assertEqual((partial['content'], partial['matchedPages']), ('第二页', [2]))
        self.assertNotIn('reference', partial)
        full = call('openMemorySource', **ref)
        self.assertEqual(full['content'], body)
        call('updateLongTermReference', reference=full['reference'], content=body, summary='新摘要')
        observed = call('openMemorySource', id=full['reference'])
        self.assertEqual(observed['summary'], '新摘要')
        self.assertNotEqual(observed['reference'], full['reference'])
        store.update(self.db, ref['id'], 2, title='管理员改了标题')
        with self.assertRaisesRegex(Exception, '重读'):
            call('updateLongTermReference', reference=observed['reference'], content='旧稿')
        current = call('openMemorySource', **ref)
        call('updateLongTermReference', reference=current['reference'], content='修订正文')
        saved = call('openMemorySource', **ref)
        self.assertEqual((saved['title'], saved['summary']), ('管理员改了标题', '新摘要'))

        uid = '12345678-1234-4012-8000-abcdef000000'
        original = store.create(self.db, '信息卡', '旧卡', '旧正文', source_id='aru-card-' + uid)
        self.assertEqual(call('openMemorySource', id='memory_event:' + uid.upper())['id'], original)
        self.assertEqual(call('openMemorySource', id='card:12345678')['id'], original)
        store.create(self.db, '信息卡', '同前缀的卡', '另一个正文', source_id='aru-card-' + uid[:-1] + '1')
        with self.assertRaisesRegex(Exception, '未唯一'):
            call('openMemorySource', id='card:12345678')

        imported = store.create(self.db, '长期资料', '从前写的', '原文', source_id='aru-reference-' + uid)
        with store.connect(self.db, write=True) as db:
            db.executescript("""
                CREATE TABLE raw_events(id, event_hash, source, session_id);
                CREATE TABLE aru_scope(session, allowed);
            """)
            db.execute('INSERT INTO companion_material_origin VALUES (?,?)', (imported, '迁入的摘要'))
        doc = call('openMemorySource', id='memory_reference_doc:' + uid)
        self.assertEqual((doc['id'], doc['summary']), (imported, '迁入的摘要'))
        call('updateLongTermReference', reference=doc['reference'], content=doc['content'], summary='')
        self.assertEqual(store.read(self.db, imported)['imported_summary'], '迁入的摘要')
        self.assertEqual(call('openMemorySource', id=imported)['summary'], '')

    def test_index_only_uses_current_text_and_injects_at_document_granularity(self):
        class Embed:
            dimension = 2
            def documents(self,texts):
                return [[1.,0.] if '柑橘' in t else [0.,1.] for t in texts]
            def query(self,q):
                return {'embedding':[1.,0.]}
        def rerank(q,docs):
            return {x['ref']:float('柑橘' in x['body']) for x in docs}
        prefix = '正文\n' * 620
        card = store.create(self.db,'信息卡','水果',prefix+'柑橘','card')
        ref = store.create(self.db,'长期资料','手册',prefix+'柑橘摘录','ref')
        with patch.object(search,'models',return_value=('profile',Embed(),rerank)):
            q='哪种果子'
            result = search.search(self.settings,q,kinds=store.CATEGORIES,limit=2,automatic=True)
            by_id = {x['id']:x for x in result['items']}
            self.assertEqual(by_id[card]['passage'],prefix+'柑橘')
            self.assertIn('柑橘摘录',by_id[ref]['passage'])
            self.assertNotEqual(by_id[ref]['passage'],prefix+'柑橘摘录')
            self.assertEqual(by_id[card]['matched_by'],['semantic'])
            store.update(self.db,card,1,content='新资料')
            after=search.search(self.settings,q,kinds=store.CATEGORIES,limit=2,automatic=True)
            self.assertEqual([x['id'] for x in after['items']],[ref])
            recalled = search.recall(self.settings, q)
            self.assertEqual(recalled['titles'], 2)
            self.assertIn('Serein MCP 的 openMemorySource(id)', recalled['context'])

if __name__=='__main__':
    unittest.main()
