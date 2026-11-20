"""Generate persistent embeddings from existing MailAgent SQLite without re-downloading mail.
Usage: python rag_index.py --once | --loop | --status
"""
import argparse
import hashlib
import logging
import os
import sqlite3
import time
from pathlib import Path
from dotenv import load_dotenv
import storage
import rag_hybrid as rag

load_dotenv(Path(__file__).resolve().parent / '.env')
log = logging.getLogger('mailagent.index')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

def state_db():
    conn = sqlite3.connect(str(Path(__file__).resolve().parent / 'rag_index_state.db'), timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('CREATE TABLE IF NOT EXISTS indexed (mid INTEGER NOT NULL, model TEXT NOT NULL, hash TEXT NOT NULL, chunks INTEGER NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(mid,model))')
    return conn

def split_mail(mail):
    # Limit extraordinary messages; each piece independently searchable.
    title = str(mail['subject'] or '')[:350]
    body = str(mail['body'] or '')[:12000]
    base = f'제목: {title}\n'
    if not body.strip():
        return [base.strip()] if title.strip() else []
    result = []
    span, overlap = 1500, 180
    step = span - overlap
    for idx, start in enumerate(range(0, len(body), step)):
        if idx >= 10: break
        result.append(base + '본문: ' + body[start:start + span])
        if start + span >= len(body): break
    return result

def index_batch(batch_size=12):
    col = rag.collection()
    with storage.connect() as db, state_db() as st:
        rows = db.execute('SELECT id,account,subject,body FROM messages ORDER BY id ASC').fetchall()
        hashes = {mid: h for mid,h in st.execute('SELECT mid,hash FROM indexed WHERE model=?',(rag.MODEL,))}
        queue=[]
        for row in rows:
            chunks=split_mail(row)
            digest=hashlib.sha256(('\n'.join(chunks)).encode('utf-8')).hexdigest()
            if hashes.get(row['id']) != digest: queue.append((row,chunks,digest))
            if len(queue)>=batch_size: break
        for row,chunks,digest in queue:
            mid = int(row['id'])
            if not chunks:
                with st: st.execute('INSERT OR REPLACE INTO indexed(mid,model,hash,chunks) VALUES(?,?,?,0)',(mid,rag.MODEL,digest))
                continue
            vectors = rag.embeddings(chunks)
            # Delete any old chunk IDs for this message, then insert fresh version. Metadata contains no text.
            col.delete(where={'mid': mid})
            col.upsert(ids=[f'm{mid}_c{i}' for i in range(len(chunks))], embeddings=vectors,
                       metadatas=[{'account': row['account'], 'mid':mid, 'chunk':i} for i in range(len(chunks))])
            with st:
                st.execute('INSERT OR REPLACE INTO indexed(mid,model,hash,chunks) VALUES(?,?,?,?)',(mid,rag.MODEL,digest,len(chunks)))
            log.info('Embedded mail id=%s chunks=%s', mid, len(chunks))
        return len(queue), len(rows)

def status():
    with storage.connect() as db, state_db() as st:
        total=db.execute('SELECT count(*) FROM messages').fetchone()[0]
        done=st.execute('SELECT count(*) FROM indexed WHERE model=?',(rag.MODEL,)).fetchone()[0]
    print(f'Model: {rag.MODEL} | SQLite mails: {total} | indexed tracking entries: {done} | remaining >= {max(0,total-done)}')

def main():
    parser=argparse.ArgumentParser()
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--once',action='store_true',help='index all currently missing/changed mail then exit')
    mode.add_argument('--loop',action='store_true',help='keep indexing new mail')
    mode.add_argument('--status',action='store_true')
    args=parser.parse_args()
    if args.status:
        status();return
    while True:
        try:
            count,total=index_batch()
            if not count:
                if args.once: break
                time.sleep(30)
            else:
                time.sleep(0.2)
        except KeyboardInterrupt: break
        except Exception:
            log.exception('Embedding index failed; retrying')
            if args.once: raise
            time.sleep(30)
    status()

if __name__=='__main__': main()
