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
            self.assertEqual(search.recall(self.settings,q)['titles'],2)

if __name__=='__main__':
    unittest.main()
