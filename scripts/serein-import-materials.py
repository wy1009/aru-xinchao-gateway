"""One-time handover of authored Aru materials. Existing Serein edits remain authoritative."""
import json
import os
from pathlib import Path
import pwd

from serein_library.store import RemovedImport, SourceUnavailable, create, read, setup, connect

def migrate():
    from curated_pages import read_sources, MCPClient
    pages = read_sources()
    client = MCPClient('http://127.0.0.1:18458/mcp/gushou/',
                       'Bearer '+Path('/etc/gushou-hindsight/local-key').read_text().strip())
    docs = []
    try:
        folders = client.tool('get_page_tree', {})['roots']
        authored = {(f['name'],n['name']): n for f in folders if f['kind']=='folder'
                    and f['name'] in ('信息卡','长期资料') for n in f['children'] if n['kind']=='page'}
        if set(authored) != {(p['kind'],p['name']) for p in pages}:
            raise ValueError('Current authored catalog differs from the Aru import; reconcile before cutover')
        for p in pages:
            current = client.tool('get_page',{'page_id':authored[p['kind'],p['name']]['id']})
            if not all(block in current['markdown'] for block in p['pieces']):
                raise ValueError('Current page differs from original; reconcile latest edit before cutover')
            blocks = [b for b in p['pieces'] if not b.startswith('### 有效原话来源\n用 get_document ')]
            summary = ''
            if p['kind']=='长期资料':
                body = p['original']
                if len(blocks)>1 and blocks[0].startswith('## 摘要\n\n'):
                    summary = blocks[0].removeprefix('## 摘要\n\n')
            else:
                body = '\n\n'.join(blocks)
            docs.append((p['mid'],p['kind'],p['name'],body,summary,p['evidence']))
    finally:
        client.close()
    user = pwd.getpwnam('gushou-serein')
    os.setgroups([])
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    path = '/var/lib/gushou-serein/serein.db'
    setup(path)
    counts = {'信息卡': 0, '长期资料': 0}
    for source_id,kind,title,body,summary,evidence in docs:
        with connect(path) as db:
            prior = db.execute('SELECT id FROM companion_material WHERE source_id=?',(source_id,)).fetchone()
        try:
            identifier = prior[0] if prior else create(path,kind,title,body,'seed:'+source_id,source_id=source_id)
            saved = read(path,identifier)
        except (RemovedImport, SourceUnavailable):
            continue
        if saved['revision']==1 and (title,body)!=(saved['title'],saved['content']):
            raise ValueError('Full text verification failed')
        with connect(path,write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM companion_material WHERE id=?', (identifier,)).fetchone():
                continue
            db.execute('INSERT OR IGNORE INTO companion_material_origin VALUES(?,?)',(identifier,summary))
            for _,_,mid in evidence:
                db.execute("""INSERT OR IGNORE INTO companion_material_source SELECT ?,r.id,r.event_hash
                    FROM raw_events r JOIN aru_scope a ON a.session=r.session_id AND a.allowed=1
                    WHERE source='aru' AND source_event_id=?""",(identifier,mid))
        counts[kind] += 1
    print(json.dumps(counts,ensure_ascii=False))

if __name__ == '__main__':
    migrate()
