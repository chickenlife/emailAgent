"""Hybrid RRF retrieval: private Chroma vector collection + existing FTS5.
Falls back to lexical retrieval on embedding failure; no network besides local Ollama.
"""
import logging
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

import storage

load_dotenv(Path(__file__).resolve().parent / '.env')
LOG = logging.getLogger('mailagent.rag')
ROOT = Path(__file__).resolve().parent
MODEL = os.getenv('EMBED_MODEL', 'qwen3-embedding:0.6b')
OLLAMA = os.getenv('OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
ENABLED = os.getenv('RAG_VECTOR_ENABLED', 'false').lower() == 'true'

def model_collection():
    return 'mail_' + re.sub(r'[^a-z0-9_]', '_', MODEL.lower())[:54]

def collection():
    import chromadb
    cli = chromadb.PersistentClient(path=str(ROOT / 'rag_vectors'))
    return cli.get_or_create_collection(name=model_collection(), metadata={'hnsw:space': 'cosine'})

def embeddings(texts):
    r = requests.post(OLLAMA + '/api/embed', json={'model': MODEL, 'input': texts, 'truncate': True}, timeout=(5, 150))
    r.raise_for_status()
    result = r.json()['embeddings']
    if len(result) != len(texts):
        raise ValueError('Ollama embedding count mismatch')
    return result

def retrieve(account, subject, body, exclude_id, limit=8):
    lexical = storage.history_retrieve(account, subject, body, exclude_id, max(30, limit))
    if not ENABLED:
        return lexical[:limit]
    try:
        col = collection()
        if not col.count():
            return lexical[:limit]
        query = ('제목: ' + (subject or '') + '\n본문: ' + (body or '')[:1400]).strip()
        vectors = embeddings([query])
        found = col.query(query_embeddings=vectors, n_results=min(60, col.count()),
                          where={'account': account}, include=['metadatas', 'distances'])
        ids = []
        for meta in found['metadatas'][0]:
            mid = int(meta['mid'])
            if mid != int(exclude_id) and mid not in ids:
                ids.append(mid)
        # Reciprocal Rank Fusion with per-list weight; candidate IDs loaded from SQLite (not Chroma documents).
        scores = {}
        for rank, r in enumerate(lexical):
            scores[r['id']] = scores.get(r['id'], 0.0) + 1.0 / (60 + rank + 1)
        for rank, mid in enumerate(ids):
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (60 + rank + 1)
        order = sorted(scores, key=scores.get, reverse=True)[:max(16, limit)]
        if not order:
            return lexical[:limit]
        with storage.connect() as db:
            marks = ','.join('?' for _ in order)
            rows = db.execute('SELECT id,role,subject,sender,recipients,cc,date_text,body,folder FROM messages '
                              f'WHERE account=? AND id IN ({marks})', [account, *order]).fetchall()
        mapping = {r['id']: dict(r) for r in rows}
        LOG.info('Hybrid RAG: account=%s lexical=%s vector=%s merged=%s', account, len(lexical), len(ids), len(order))
        return [mapping[mid] for mid in order if mid in mapping][:limit]
    except Exception as exc:
        LOG.warning('Vector RAG unavailable (%s): lexical fallback', exc)
        return lexical[:limit]
