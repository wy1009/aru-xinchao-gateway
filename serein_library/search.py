"""Hybrid discovery over current Serein sources; automatic queries only add authored materials."""
from array import array
from collections import Counter
import hashlib
import json
import logging
import re
from functools import lru_cache
from itertools import islice
from threading import Lock

from .store import CATEGORIES, SourceUnavailable, catalog, connect, passages, read, sources

KINDS = (*CATEGORIES, '事件', '原文')
STRONG_RELEVANCE = 0.5
_index_locks = {True: Lock(), False: Lock()}
log = logging.getLogger(__name__)


def models(settings):
    from serein.configured_models import effective_settings
    from serein.adapters.embedding import EmbeddingClient
    from serein.adapters.reranker import RerankerClient
    cfg = effective_settings(settings)
    embed = EmbeddingClient(cfg.database, cfg.index, **cfg.embedding)
    profile = hashlib.sha256(json.dumps([embed.profile, embed.dimension], sort_keys=True).encode()).hexdigest()
    return profile, embed, RerankerClient(**cfg.reranker)


def chunks(path, kinds):
    with connect(path) as db:
        return {(ident, stamp, n): (kind, title, part)
                for ident, kind, title, body, stamp in sources(db, kinds)
                for n, part in enumerate(passages(body)) if part.strip()}


def fill(path, profile, embed, corpus, batch_size=8):
    from serein.recall.embedding_batches import request_batches
    materials_only = all(row[0] in CATEGORIES for row in corpus.values())
    with _index_locks[materials_only]:
        with connect(path) as db:
            have = {tuple(r) for r in db.execute(
                'SELECT id,stamp,part FROM companion_search_vector WHERE profile=?', (profile,))}
        missing = [{'key':key, 'input': title + '\n' + body}
                   for key, (_, title, body) in corpus.items() if key not in have]
        for batch, vectors in request_batches(embed, missing, batch_size=batch_size, concurrency=2):
            with connect(path, write=True) as db:
                db.executemany('INSERT OR IGNORE INTO companion_search_vector VALUES (?,?,?,?,?)',
                               [(key[0],key[1],profile,key[2],array('f',vec).tobytes())
                                for item,vec in zip(batch,vectors) for key in [item['key']]])
        return len(missing)


@lru_cache(maxsize=5000)
def terms(text):
    words = set()
    for token in re.findall(r'[\u3400-\u9fff]+|[\w]+',text.casefold()):
        if '\u3400' <= token[0] <= '\u9fff':
            if len(token)==1:
                words.add(token)
            else:
                words.update(token[i:i+2] for i in range(len(token)-1))
        else:
            words.add(token)
    return frozenset(words)


def ranks(query, corpus, vectors, query_vec):
    import math
    words = terms(query)
    keys = list(corpus)
    lexical = {}
    frequencies = Counter()
    matches = {}
    for k, (_, title, body) in corpus.items():
        title_words = words & terms(title)
        body_words = words & terms(body)
        frequencies.update(title_words | body_words)
        matches[k] = (title_words, body_words)
    weights = {w:math.log(1 + len(keys)/(1+n)) for w,n in frequencies.items()}
    for k, (a,b) in matches.items():
        _,title,body = corpus[k]
        exact = query.casefold()
        score = sum(weights[w] for w in a)*2 + sum(weights[w] for w in b)
        if exact in title.casefold() or exact in body.casefold():
            score += 20
        if score:
            lexical[k] = score
    semantic = {}
    if query_vec is not None:
        for k, blob in vectors:
            if k in corpus:
                semantic[k] = sum(map(float.__mul__, memoryview(blob).cast('f'), query_vec))
    return lexical, semantic


def candidates(corpus, lexical, semantic, limit):
    """Rank each lane independently and retain representatives from all requested types."""
    results = {}
    for kind in {value[0] for value in corpus.values()}:
        groups = {}
        for name, scores in [('keyword',lexical),('semantic',semantic)]:
            ordered = sorted((k for k in scores if corpus[k][0] == kind), key=scores.get, reverse=True)
            for rank,k in enumerate(ordered[:max(20,limit*2)],1):
                g = groups.setdefault(k, {'rrf':0,'via':[]})
                g['rrf'] += 1/(25+rank)
                g['via'].append(name)
        results.update(sorted(groups.items(), key=lambda e:-e[1]['rrf'])[:max(8,limit)])
        # A decisive hit on a single lane must still reach the reranker.
        for scores, name in [(lexical,'keyword'),(semantic,'semantic')]:
            for k in islice(sorted((k for k in scores if corpus[k][0]==kind),key=scores.get,reverse=True),3):
                results.setdefault(k, groups[k])
    return results


def search(settings, query, *, kinds=KINDS, limit=10, automatic=False):
    if not query.strip() or not 1 <= limit <= 30 or not kinds or set(kinds)-set(KINDS):
        raise ValueError("请提供搜索词、有效类型和 1 到 30 之间的条数")
    query = query.strip()
    path = settings.database
    corpus = chunks(path, kinds)
    if not corpus:
        return {'items':[], 'warnings':[]}
    warnings = []
    query_vec = None
    vectors = []
    rerank = None
    try:
        profile, embed, rerank = models(settings)
        try:
            fill(path,profile,embed,corpus)
        except (OSError, ValueError) as exc:
            log.warning("Serein library indexing incomplete (%s)",type(exc).__name__)
            warnings.append('部分新内容尚未完成向量索引')
        query_vec = embed.query(query)['embedding']
        with connect(path) as db:
            sql = 'SELECT id,stamp,part,embedding FROM companion_search_vector WHERE profile=?'
            args = [profile]
            if set(kinds) <= set(CATEGORIES):
                sql += ' AND id IN (SELECT id FROM companion_material)'
            vectors = [((r[0],r[1],r[2]),r[3]) for r in db.execute(sql,args)]
    except Exception as exc:
        log.warning("Serein library embedding unavailable (%s)",type(exc).__name__)
        warnings.append('向量检索未完成')
    lexical, semantic = ranks(query,corpus,vectors,query_vec)
    pool = candidates(corpus,lexical,semantic,limit)
    scores = {}
    if pool and rerank:
        try:
            # Multi-topic information cards can match a single passage closely.
            # Score every candidate passage, plus one full-card input when it fits.
            # Serein's reranker keeps the best score for each ref.
            seen_cards = set()
            documents = []
            for k in pool:
                kind,title,body = corpus[k]
                documents.append({'ref':k,'title':title,'body':body})
                if kind == '信息卡' and k[0] not in seen_cards:
                    seen_cards.add(k[0])
                    try:
                        full = read(path,k[0])['content']
                    except SourceUnavailable:
                        continue
                    if full != body and len(full) <= 3000:
                        documents.append({'ref':k,'title':title,'body':full})
            scores = rerank(query,documents)
            if set(scores) != set(pool):
                raise ValueError('Incomplete rerank response')
        except Exception as exc:
            log.warning("Serein library rerank unavailable (%s)",type(exc).__name__)
            warnings.append('重排未完成')
            scores = {}
    order = sorted(pool, key=lambda k:(scores.get(k,0),pool[k]['rrf']),reverse=True)
    # Large chat archives can supply dozens of near-identical replies to a single
    # question. Give each strongly relevant source type one result before filling
    # the remaining manual slots in score order.
    if not automatic and len(kinds)>1 and scores:
        leaders = {}
        for k in order:
            if scores[k] >= STRONG_RELEVANCE:
                leaders.setdefault(corpus[k][0], k)
        head = set(leaders.values())
        order = list(leaders.values()) + [k for k in order if k not in head]
    items = []
    seen = set()
    # Validate live identity and revision after network calls. Never send withdrawn content.
    current = chunks(path,kinds)
    for k in order:
        identifier, stamp, n = k
        if k not in current or current[k] != corpus[k] or identifier in seen:
            continue
        kind,title,body = current[k]
        if automatic and (not scores or scores[k] < STRONG_RELEVANCE):
            continue
        item = {'id':identifier,'kind':kind,'title':title,'passage':body,'part':n+1,
                'matched_by':pool[k]['via'],'relevance':scores.get(k)}
        if kind in CATEGORIES:
            try:
                doc = read(path,identifier)
            except SourceUnavailable:
                continue
            if str(doc['revision']) != stamp:
                continue
            item['revision'] = doc['revision']
            if automatic and kind == '信息卡':
                item['passage'] = doc['content']
        seen.add(identifier)
        items.append(item)
        if len(items) == limit:
            break
    return {'items':items,'warnings':warnings}


def recall(settings, query):
    hits = search(settings,query,kinds=CATEGORIES,limit=2,automatic=True)
    directory = catalog(settings.database)
    current = {d['id']: d['revision'] for d in directory}
    items = [r for r in hits['items'] if current.get(r['id']) == r['revision']]
    lines = ['[Serein 资料目录 · 标题与 ID；用 Serein MCP 的 openMemorySource(id) 读全文；更新时沿用返回的 cardId 或 reference]']
    lines.extend(json.dumps({k:d[k] for k in ('kind','title','id')},ensure_ascii=False) for d in directory)
    lines.append('[/Serein 资料目录]')
    if items:
        lines.append('[Serein 命中资料 · 历史资料，仅在相关时参考]')
        for r in items:
            lines.append(json.dumps({k:r[k] for k in ('id','kind','title','revision','passage')},ensure_ascii=False))
        lines.append('[/Serein 命中资料]')
    return {'context':'\n'.join(lines) if directory else '', 'count':len(items),
            'titles':len(directory),'warnings':hits['warnings']}
