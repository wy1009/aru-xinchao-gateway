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

    def test_admin_deletion_checks_revision_and_removes_only_the_selected_material(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from serein_library.api import routes
        app = FastAPI()
        app.include_router(routes(self.settings, []))
        client = TestClient(app)
        for kind in store.CATEGORIES:
            with self.subTest(kind=kind):
                item = store.create(self.db, kind, '测试', '原始正文', source_id=kind)
                url = '/v1/library/materials/' + item
                keep = store.create(self.db, kind, '同名', '单独保留')
                store.update(self.db, item, 1, content='编辑后的正文')
                with store.connect(self.db, write=True) as db:
                    db.execute('INSERT INTO companion_material_origin VALUES (?,?)', (item, '迁入时的摘要'))
                    db.execute('INSERT INTO companion_material_summary VALUES (?,?)', (item, '摘要'))
                    db.execute('INSERT INTO companion_material_source VALUES (?,1,?)', (item, 'hash'))
                    db.execute('INSERT INTO companion_search_vector VALUES (?,2,?,0,?)', (item, 'p', b'vec'))
                self.assertEqual(client.delete(url).status_code, 422)
                def remove(revision):
                    return client.request('DELETE', url, json={'expected_revision':revision})
                self.assertEqual(remove(1).status_code, 409)
                self.assertIn(item, [r['id'] for r in store.catalog(self.db)])
                self.assertEqual(remove(2).json(), {'id':item, 'deleted':True})
                self.assertEqual(remove(2).json(), {'id':item, 'deleted':False})
                with self.assertRaises(store.SourceUnavailable):
                    store.read(self.db, item)
                with self.assertRaises(store.RemovedImport):
                    store.create(self.db, kind, '测试', '原始正文', source_id=kind)
                with store.connect(self.db, write=True) as db:
                    # A pending embedding from before deletion finishes after the admin request.
                    db.execute('INSERT INTO companion_search_vector VALUES (?,2,?,0,?)', (item, 'p', b'late'))
                    for table in ('companion_search_vector', 'companion_material_source',
                                  'companion_material_summary', 'companion_material_origin'):
                        self.assertFalse(db.execute(f'SELECT 1 FROM {table} WHERE id=?', (item,)).fetchone())
                self.assertNotIn(item, [r['id'] for r in store.catalog(self.db)])
                self.assertEqual(store.read(self.db, keep)['content'], '单独保留')

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

    def test_material_history_is_bounded_and_restores_without_overwriting_edits(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from serein_library.api import routes
        app = FastAPI()
        app.include_router(routes(self.settings, []))
        client = TestClient(app)
        server = self.material_server()

        def call(name, **args):
            return asyncio.run(server.call_tool(name, args))[1]

        for kind in store.CATEGORIES:
            with self.subTest(kind=kind):
                doc = store.create(self.db, kind, '标题甲', '甲\n乙\n')
                url = '/v1/library/materials/' + doc
                first = client.get(url + '/history').json()
                self.assertEqual((first['versions'], first['selected_revision']), ([], None))
                if kind == '长期资料':
                    with store.connect(self.db, write=True) as db:
                        db.execute('INSERT INTO companion_material_origin VALUES (?,?)', (doc, '迁入摘要'))
                    store.update(self.db, doc, 1, title='标题乙', content='甲\n丙\n', summary='')
                else:
                    handle = call('openMemorySource', id=doc)['cardId']
                    call('writeInformationCard', cardId=handle, subject='标题乙', content='甲\n丙\n')
                diff = client.get(url + '/history').json()
                fields = {d['field']:(d['before'], d['after']) for d in diff['changes']}
                self.assertEqual(fields['content'], ('乙\n', '丙\n'))
                self.assertEqual(fields['title'], ('标题甲', '标题乙'))
                if kind == '长期资料':
                    self.assertEqual(fields['summary'], ('迁入摘要',''))
                self.assertEqual((diff['selected_revision'], diff['current_revision']), (1, 2))
                store.update(self.db, doc, 2, content='甲\n丙\n')
                store.update(self.db, doc, 2, content='第三稿')
                def restore(target, expected):
                    return client.post(f'{url}/history/{target}', json={'expected_revision':expected})
                self.assertEqual(restore(1, 2).status_code, 409)
                self.assertEqual(restore(1, 3).json()['revision'], 4)
                self.assertFalse(client.get(url+'/history/1').json()['changes'])
                with store.connect(self.db) as db:
                    restored = db.execute('SELECT title,content FROM companion_material WHERE id=?',(doc,)).fetchone()
                    self.assertEqual(tuple(restored), ('标题甲', '甲\n乙\n'))
                    if kind == '长期资料':
                        self.assertEqual(db.execute('SELECT summary FROM companion_material_summary WHERE id=?',
                                                    (doc,)).fetchone()[0], '迁入摘要')
                self.assertEqual(restore(3, 4).json()['revision'], 5)
                for rev in range(5, 10):
                    store.update(self.db, doc, rev, content=f'正文{rev}')
                kept = client.get(url+'/history').json()['versions']
                self.assertEqual([entry['revision'] for entry in kept], [9,8,7,6,5])
                self.assertEqual(client.get(url+'/history/4').status_code, 404)
                self.assertEqual(restore(4, 10).status_code, 404)
                store.delete(self.db, doc, 10)
                self.assertEqual(client.get(url+'/history').status_code, 404)
                with store.connect(self.db) as db:
                    self.assertFalse(db.execute('SELECT 1 FROM companion_material_history WHERE id=?',(doc,)).fetchone())

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

        opaque = 'mem_9876543210abcdef9876543210ab'
        card = store.create(self.db, '信息卡', '迁入的卡', '现行卡片', source_id='aru-card-' + opaque)
        self.assertEqual(call('openMemorySource', id='memory_event:' + opaque)['id'], card)
        with self.assertRaisesRegex(Exception, '未唯一'):
            call('openMemorySource', id='memory_event:' + opaque.upper())

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

    def test_search_hits_have_one_open_action_and_expand_current_evidence(self):
        from serein.core.store import Store, encode
        self.settings.database = Path(self.folder.name) / 'native.db'
        path = self.settings.database
        with Store(path) as native:
            db = native.conn
            db.executescript("""
                CREATE TABLE raw_events(id INTEGER PRIMARY KEY,source,session_id,source_event_id,role,
                    text,event_hash,created_at,metadata_json);
                CREATE TABLE aru_scope(session PRIMARY KEY, allowed);
                CREATE TABLE aru_original(source,session,mid,role,text,stamp,seq);
                INSERT INTO aru_scope VALUES('window',1);
            """)
            native.create('one', 'event', '旧花园', '花园里种薄荷。')
            native.create('two', 'scene', '花园日记', '花园里下雨了。')
            for number, role, body in [(2, 'assistant', '花园种薄荷。'), (1, 'user', '花园种什么？')]:
                mid = str(number)
                db.execute('INSERT INTO raw_events VALUES(?,?,?,?,?,?,?,?,?)',
                           (number, 'aru', 'window', mid, role, body, mid, '2025-01-01T00:00:00Z', '{}'))
                db.execute('INSERT INTO aru_original VALUES(?,?,?,?,?,?,?)',
                           ('aru', 'window', mid, role, body, number, number))
                src = native.add_source(encode(['aru', 'window', mid]), body)
                native.bind('one', src)
        store.setup(path)
        card = store.create(path, '信息卡', '花园卡', '花园信息')
        ref = store.create(path, '长期资料', '花园书', '花园资料')
        server = self.material_server()

        def call(tool, **args):
            return asyncio.run(server.call_tool(tool, args))[1]

        class Embed:
            dimension = 1
            def documents(self, texts):
                return [[1.] for _ in texts]
            def query(self, text):
                return {'embedding': [1.]}

        def rerank(query, docs):
            return {item['ref']: .95 if item['ref'][0] == 'event:one' else .7 for item in docs}

        with patch.object(search, 'models', return_value=('test', Embed(), rerank)):
            hits = call('search_library', query='花园')['items']
            self.assertEqual({h['id'] for h in hits}, {'event:one', 'scene:two', 'raw:1', 'raw:2', card, ref})
            self.assertEqual([h['id'] for h in hits if 'opened' in h], ['event:one'])
            for hit in hits:
                action = hit['open']
                self.assertEqual(action, {'name':'openMemorySource', 'arguments':{'id':hit['id']}})
                doc = call(action['name'], **action['arguments'])
                self.assertIn(hit['passage'], doc['content'])
                if 'opened' in hit:
                    self.assertEqual(doc, hit['opened'])
                    self.assertEqual([(x['id'], x['speaker'], x['content']) for x in doc['evidence']],
                                     [('raw:1', '主人', '花园种什么？'), ('raw:2', '顾守', '花园种薄荷。')])
            self.assertEqual(call('openMemorySource', id='aru:window:1'), call('openMemorySource', id='raw:1'))
            self.assertEqual(call('openMemorySource', id='scene:two')['evidence'], [])
            with store.connect(path, write=True) as db:
                db.execute("UPDATE aru_original SET text='改过的原话' WHERE mid='1'")
            for ident in ('event:one', 'raw:1'):
                with self.assertRaisesRegex(Exception, '重新搜索'):
                    call('openMemorySource', id=ident)
            self.assertNotIn('event:one', {h['id'] for h in call('search_library', query='花园')['items']})
            with store.connect(path, write=True) as db:
                db.execute('UPDATE aru_scope SET allowed=0')
            with self.assertRaisesRegex(Exception, '重新搜索'):
                call('openMemorySource', id='raw:2')
            with Store(path) as native:
                native.set_lifecycle('two', 'archived')
            with self.assertRaisesRegex(Exception, '重新搜索'):
                call('openMemorySource', id='scene:two')

        with patch.object(search, 'models', return_value=('test', Embed(), lambda q, docs: {
                d['ref']: .1 for d in docs})):
            hits = call('search_library', query='花园', kind='信息卡')['items']
            self.assertEqual(len(hits), 1)
            self.assertIn('open', hits[0])
            self.assertNotIn('opened', hits[0])

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

        def delete_during_rerank(q, docs):
            store.delete(self.db, ref, 1)
            return rerank(q, docs)
        with patch.object(search, 'models', return_value=('profile', Embed(), delete_during_rerank)):
            recalled = search.recall(self.settings, q)
            self.assertEqual((recalled['titles'], recalled['count']), (1, 0))
            self.assertNotIn(ref, recalled['context'])

if __name__=='__main__':
    unittest.main()
