"""SQLite, full text lexical RAG with Korean bigram expansion."""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from email.utils import getaddresses

DB = Path(__file__).resolve().parent / 'mailagent.db'


@contextmanager
def connect():
    """Commit or roll back and ALWAYS close SQLite connection (Windows safe)."""
    db = sqlite3.connect(DB, timeout=30)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=30000')
        with db:
            yield db
    finally:
        db.close()


def initialize():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity TEXT NOT NULL,
          uid INTEGER NOT NULL, role TEXT NOT NULL, msgid TEXT,
          subject TEXT, sender TEXT, recipients TEXT, cc TEXT,
          date_text TEXT, body TEXT, is_unread INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(account,folder,uidvalidity,uid)
        );
        CREATE TABLE IF NOT EXISTS draft_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id INTEGER UNIQUE NOT NULL REFERENCES messages(id),
          draft TEXT NOT NULL DEFAULT '', sources TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'pending', deleted INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS scan_state (
          account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity TEXT NOT NULL,
          highest_uid INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(account, folder)
        );
        CREATE TABLE IF NOT EXISTS historical_backfill (
          account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity TEXT NOT NULL,
          next_before_uid INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(account,folder)
        );
        CREATE TABLE IF NOT EXISTS scan_failures (
          account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity TEXT NOT NULL,
          uid INTEGER NOT NULL, role TEXT NOT NULL, notify_on_recovery INTEGER NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
          next_retry_at REAL NOT NULL DEFAULT 0, last_attempt_at REAL NOT NULL DEFAULT 0,
          PRIMARY KEY(account,folder,uidvalidity,uid)
        );
        CREATE INDEX IF NOT EXISTS scan_failures_due ON scan_failures(account,folder,next_retry_at);
        CREATE TABLE IF NOT EXISTS reply_suggestions (
          message_id INTEGER PRIMARY KEY,
          account TEXT NOT NULL,
          model TEXT NOT NULL,
          draft TEXT NOT NULL,
          cited_ids TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL,
          error_text TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ai_mail_summaries (
          message_id INTEGER PRIMARY KEY,
          account TEXT NOT NULL,
          model TEXT NOT NULL,
          body_hash TEXT NOT NULL,
          summary_json TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reply_feedback (
          account TEXT NOT NULL, message_id INTEGER NOT NULL,
          rating TEXT NOT NULL CHECK(rating IN ('good','bad')),
          approved_text TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(account,message_id)
        );
        CREATE TABLE IF NOT EXISTS evidence_feedback (
          account TEXT NOT NULL, message_id INTEGER NOT NULL, reference_id INTEGER NOT NULL,
          rating TEXT NOT NULL CHECK(rating IN ('good','bad')),
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(account,message_id,reference_id)
        );
        CREATE INDEX IF NOT EXISTS evidence_feedback_lookup ON evidence_feedback(account,reference_id,rating);
        CREATE TABLE IF NOT EXISTS reply_style (
          account TEXT PRIMARY KEY, greeting TEXT NOT NULL DEFAULT '',
          closing TEXT NOT NULL DEFAULT '', length TEXT NOT NULL DEFAULT 'normal',
          preferred_phrases TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS message_search USING fts5(searchtext);
        ''')
        cols = {r['name'] for r in db.execute('PRAGMA table_info(draft_items)')}
        if 'retrieved_sources' not in cols:
            db.execute("ALTER TABLE draft_items ADD COLUMN retrieved_sources TEXT NOT NULL DEFAULT '[]'")
        msg_cols={r['name'] for r in db.execute('PRAGMA table_info(messages)')}
        if 'body_missing' not in msg_cols:
            db.execute('ALTER TABLE messages ADD COLUMN body_missing INTEGER NOT NULL DEFAULT 0')
        for header_col in ('in_reply_to', 'references_header'):
            if header_col not in msg_cols:
                db.execute(f"ALTER TABLE messages ADD COLUMN {header_col} TEXT NOT NULL DEFAULT ''")
        db.execute('CREATE INDEX IF NOT EXISTS ix_messages_thread_lookup ON messages(account,role,msgid)')


def grams(value: str):
    words = re.findall(r'[가-힣]+|[a-zA-Z0-9]+', (value or '').lower())
    out = []
    for word in words:
        if len(word) > 1:
            out.append(word)
        if re.search(r'[가-힣]',word):
            out.extend(word[i:i+2] for i in range(len(word)-1))
    return list(dict.fromkeys(out))


def index_text(msg):
    text = ' '.join(str(msg.get(k) or '') for k in ('subject','sender','recipients','cc','body'))
    return text + '\n' + ' '.join(grams(text))


def upsert(account, folder, uidvalidity, uid, role, msg):
    with connect() as db:
        db.execute('''INSERT INTO messages
            (account,folder,uidvalidity,uid,role,msgid,subject,sender,recipients,cc,date_text,body,is_unread,in_reply_to,references_header)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account,folder,uidvalidity,uid) DO UPDATE SET
              msgid=excluded.msgid, subject=excluded.subject, sender=excluded.sender,
              recipients=excluded.recipients,cc=excluded.cc,date_text=excluded.date_text,
              in_reply_to=COALESCE(NULLIF(excluded.in_reply_to,''),messages.in_reply_to),
              references_header=COALESCE(NULLIF(excluded.references_header,''),messages.references_header),
              body=CASE WHEN length(trim(coalesce(excluded.body,'')))>0 THEN excluded.body ELSE messages.body END,is_unread=excluded.is_unread''',
            (account,folder,str(uidvalidity),uid,role,msg.get('msgid'),msg.get('subject'),
             msg.get('sender'),msg.get('recipients'),msg.get('cc'),msg.get('date_text'),
             msg.get('body'),int(msg.get('is_unread',False)),msg.get('in_reply_to',''),msg.get('references','')))
        row = db.execute('''SELECT id FROM messages WHERE account=? AND folder=? AND uidvalidity=? AND uid=?''',
                         (account,folder,str(uidvalidity),uid)).fetchone()
        mid = row['id']
        # Index the persisted row, not the incomplete fetch response. A header-only
        # refresh must never silently remove previously indexed body text.
        persisted=db.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
        db.execute('DELETE FROM message_search WHERE rowid=?',(mid,))
        db.execute('INSERT INTO message_search(rowid,searchtext) VALUES(?,?)',(mid,index_text(dict(persisted))))
        db.execute('UPDATE messages SET body_missing=? WHERE id=?',
                   (int(not bool((persisted['body'] or '').strip())),mid))
        return mid


def state(account,folder):
    with connect() as db:
        row=db.execute('SELECT * FROM scan_state WHERE account=? AND folder=?',(account,folder)).fetchone()
        return dict(row) if row else None


def set_state(account,folder,uidvalidity,highest):
    with connect() as db:
        db.execute('''INSERT INTO scan_state VALUES(?,?,?,?) ON CONFLICT(account,folder) DO UPDATE
        SET uidvalidity=excluded.uidvalidity,highest_uid=excluded.highest_uid''',
        (account,folder,str(uidvalidity),int(highest)))


def historical_backfill_state(account, folder, uidvalidity):
    with connect() as db:
        row=db.execute('''SELECT * FROM historical_backfill
            WHERE account=? AND folder=? AND uidvalidity=?''',
            (account,folder,str(uidvalidity))).fetchone()
        return dict(row) if row else None


def set_historical_backfill(account, folder, uidvalidity, before_uid, completed=False):
    with connect() as db:
        db.execute('''INSERT INTO historical_backfill
            (account,folder,uidvalidity,next_before_uid,completed) VALUES(?,?,?,?,?)
            ON CONFLICT(account,folder) DO UPDATE SET
            uidvalidity=excluded.uidvalidity,
            next_before_uid=excluded.next_before_uid,
            completed=excluded.completed''',
            (account,folder,str(uidvalidity),int(before_uid),int(bool(completed))))


def backfill_progress(account):
    with connect() as db:
        rows=db.execute('''SELECT folder,completed,next_before_uid
            FROM historical_backfill WHERE account=? ORDER BY folder''',(account,))
        return [dict(row) for row in rows]


def create_draft(mid):
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO draft_items(message_id) VALUES(?)',(mid,))
        row=db.execute('SELECT id FROM draft_items WHERE message_id=?',(mid,)).fetchone()
        return row['id']


def save_draft(did, text, sources, status, retrieved_sources=None):
    """Persist AI-selected citations and separately retrieved RAG candidates."""
    with connect() as db:
        db.execute("""UPDATE draft_items
            SET draft=?,sources=?,retrieved_sources=?,status=? WHERE id=?""",
            (text, json.dumps(sources, ensure_ascii=False),
             json.dumps(retrieved_sources or [], ensure_ascii=False),status,did))


def _source_rows(db, account, ids):
    """Only return source mails belonging to the current IMAP account."""
    if not ids: return []
    ids = [int(x) for x in ids]
    marks = ','.join('?' for _ in ids)
    rows = db.execute(f"""SELECT id,role,msgid,subject,sender,recipients,cc,date_text,body,folder
        FROM messages WHERE account=? AND id IN ({marks})""",[account, *ids]).fetchall()
    mapping={int(x['id']):dict(x) for x in rows}
    return [mapping[x] for x in ids if x in mapping]


def get_draft(did,account=None):
    with connect() as db:
        r=db.execute("""SELECT d.*,m.account,m.subject,m.sender,m.recipients,m.cc,m.body,m.date_text,
                               m.is_unread FROM draft_items d JOIN messages m ON m.id=d.message_id
                               WHERE d.id=? AND d.deleted=0 AND (? IS NULL OR m.account=?)""",(did,account,account)).fetchone()
        if not r: return None
        result=dict(r)
        cited=json.loads(result.get('sources') or '[]')
        candidates=json.loads(result.get('retrieved_sources') or '[]')
        result['sources']=cited
        result['retrieved_sources']=candidates
        result['evidence']=_source_rows(db,result['account'],cited)
        result['retrieved_evidence']=_source_rows(db,result['account'],candidates)
        return result


def list_drafts(account=None):
    with connect() as db:
        return [dict(r) for r in db.execute('''SELECT d.id,d.status,d.created_at,m.subject,m.sender,
                    m.is_unread FROM draft_items d JOIN messages m ON m.id=d.message_id
                    WHERE d.deleted=0 AND (? IS NULL OR m.account=?) ORDER BY d.id DESC LIMIT 200''',(account,account)).fetchall()]


def delete_draft(did, account=None):
    # Hide an app item without deleting the IMAP original.
    with connect() as db:
        result=db.execute('''UPDATE draft_items SET deleted=1 WHERE id=? AND deleted=0
            AND (? IS NULL OR EXISTS (SELECT 1 FROM messages m
                 WHERE m.id=draft_items.message_id AND m.account=?))''',
            (did,account,account))
        return result.rowcount


def delete_drafts(account, ids):
    # Atomically hide only items owned by current mailbox.
    ids=list(dict.fromkeys(int(x) for x in ids))
    if not account or not ids or len(ids)>200 or any(x<=0 for x in ids):
        raise ValueError('Invalid bulk delete request')
    marks=','.join('?' for _ in ids)
    with connect() as db:
        rows=db.execute(f'''SELECT d.id FROM draft_items d JOIN messages m ON m.id=d.message_id
            WHERE m.account=? AND d.deleted=0 AND d.id IN ({marks})''',[account,*ids]).fetchall()
        actual=[int(r['id']) for r in rows]
        if actual:
            actual_marks=','.join('?' for _ in actual)
            db.execute(f'UPDATE draft_items SET deleted=1 WHERE id IN ({actual_marks})',actual)
        return actual


def history_retrieve(account, subject, body, exclude_id, limit=8):
    """Retrieve across every locally indexed inbound and outbound message.

    Rank using message subject + body text and a small bias for prior sent
    answers. Retrieval is lexical, NOT a semantic/vector embedding model.
    """
    subject = re.sub(r'^(?:(?:re|fw|fwd)\s*:\s*)+', '', subject or '', flags=re.I)
    subject_terms = grams(subject)[:18]
    body_terms = grams((body or '')[:1200])[:18]
    # Limit terms to prevent very common short body tokens dominating results.
    terms=list(dict.fromkeys(t for t in subject_terms+body_terms if len(t)>=2))[:24]
    if not terms: return []
    query=' OR '.join('"'+x.replace('"','')+'"' for x in terms)
    with connect() as db:
        results=db.execute("""SELECT m.id,m.role,m.msgid,m.subject,m.sender,m.recipients,m.cc,
                            m.date_text,m.body,m.folder,
                            bm25(message_search, 1.0) AS score
                            FROM message_search JOIN messages m ON m.id=message_search.rowid
                            WHERE message_search MATCH ? AND m.account=? AND m.id<>?
                            ORDER BY score ASC LIMIT 160""",(query,account,exclude_id)).fetchall()
    ranked=[]
    subj=set(grams(subject))
    for r in results:
        item=dict(r)
        title=set(grams(re.sub(r'^(?:(?:re|fw|fwd)\s*:\s*)+', '',r['subject'] or '', flags=re.I)))
        common=len(subj & title)
        lexical=float(item['score'] or 0)
        # Avoid presenting obviously irrelevant documents as evidence.
        if common==0 and lexical > -0.25:
            continue
        item['rank']=(common*5.0)+(1.5 if item['role']=='sent' else 0)-lexical
        ranked.append(item)
    ranked.sort(key=lambda x:x['rank'],reverse=True)
    # Include both directions where relevant, rather than always sent-only.
    chosen=ranked[:limit]
    if chosen and not any(r['role']=='inbox' for r in chosen):
        candidate=next((r for r in ranked[limit:] if r['role']=='inbox'),None)
        if candidate: chosen[-1]=candidate
    if chosen and not any(r['role']=='sent' for r in chosen):
        candidate=next((r for r in ranked[limit:] if r['role']=='sent'),None)
        if candidate: chosen[-1]=candidate
    return chosen


# Deterministic lexical retrieval. No embedding/Ollama or generated citation IDs.
STOPWORDS={
    '안녕하세요','감사합니다','확인','부탁','드립니다','관련','내용','메일','문의',
    '안내','요청','회신','검토','진행','사항','수신','발신','첨부','대하여',
    '합니다','그리고','the','for','and','this','that','from','with','please',
    'your','have','will','are','re','fwd','fw'
}

def keyword_terms(text):
    tokens=re.findall(r'[가-힣]{2,}|[a-zA-Z]{3,}|[0-9]{2,}',(text or '').lower())
    unique=[]
    for token in tokens:
        if token not in STOPWORDS and token not in unique:
            unique.append(token)
    return unique

def keyword_retrieve(account, subject, body, exclude_id, limit=10):
    """Rank exact keyword overlaps (subject > body), scoped to the mailbox account.

    Unlike the old bigram OR query, short incidental syllable matches cannot dominate.
    Returns strong_match only for at least two shared subject keywords, or all
    subject keywords for a single-term query. This is a *search match*, not AI citation.
    """
    limit=max(1,min(80,int(limit)))
    title_terms=keyword_terms(re.sub(r'^(?:(?:re|fw|fwd)\s*:\s*)+', '',subject or '',flags=re.I))[:16]
    content_terms=[t for t in keyword_terms((body or '')[:1800]) if t not in title_terms][:18]
    terms=(title_terms+content_terms)[:30]
    if not terms:return []
    # FTS indexed search is broad candidate retrieval; exact token matching reranks.
    # FTS5 stored plain content and expansion tokens, so phrase query is safe.
    fts=' OR '.join('"'+t.replace('"','')+'"' for t in terms)
    with connect() as db:
        try:
            rows=db.execute("""SELECT m.id,m.role,m.msgid,m.subject,m.sender,m.recipients,m.cc,
                m.date_text,m.body,m.folder FROM message_search
                JOIN messages m ON m.id=message_search.rowid
                WHERE message_search MATCH ? AND m.account=? AND m.id<>?
                ORDER BY bm25(message_search) ASC LIMIT 1500""",(fts,account,exclude_id)).fetchall()
        except sqlite3.OperationalError:
            return []
    ranking=[]
    for row in rows:
        result=dict(row)
        title=set(keyword_terms(result['subject']))
        body_tokens=set(keyword_terms((result['body'] or '')[:12000]))
        title_overlap=[t for t in title_terms if t in title]
        body_overlap=[t for t in title_terms if t in body_tokens]
        context_overlap=[t for t in content_terms if t in title or t in body_tokens]
        match_count=len(set(title_overlap+body_overlap+context_overlap))
        if not match_count:continue
        score=12*len(title_overlap)+4*len(body_overlap)+1.5*len(context_overlap)
        # Prevent a single common body term from appearing as strong evidence.
        if score < 3:continue
        required=1 if len(title_terms)==1 else 2
        strong=len(title_overlap)>=required and bool(title_terms)
        result['score']=round(score,2)
        result['matched_keywords']=list(dict.fromkeys(title_overlap+body_overlap+context_overlap))[:12]
        result['strong_match']=strong
        ranking.append(result)
    ranking.sort(key=lambda r:(-r['score'],-len(r['matched_keywords']),-r['id']))
    return ranking[:limit]


def rag_stats(account):
    """Display real indexed coverage; avoid claiming the entire mailbox is indexed."""
    with connect() as db:
        counts=[dict(r) for r in db.execute("""SELECT folder,role,COUNT(*) AS indexed,
             SUM(CASE WHEN length(trim(coalesce(body,''))) > 0 THEN 1 ELSE 0 END) AS with_body
             FROM messages WHERE account=? GROUP BY folder,role ORDER BY folder""",(account,))]
        return {'folders':counts, 'indexed_total':sum(x['indexed'] for x in counts),
                'sent_total':sum(x['indexed'] for x in counts if x['role']=='sent'),
                'inbox_total':sum(x['indexed'] for x in counts if x['role']=='inbox')}


def purge_non_target_drafts(account, target):
    """Remove only invalid AI draft rows. Keep every RAG message and FTS entry."""
    target = target.casefold()
    with connect() as db:
        rows = db.execute(
            """SELECT d.id, m.recipients, m.cc FROM draft_items d
               JOIN messages m ON m.id=d.message_id
               WHERE m.account=? AND m.role='inbox'""", (account,)
        ).fetchall()
        bad_ids=[]
        for row in rows:
            addresses = {addr.strip().casefold() for _,addr in
                         getaddresses([row['recipients'] or '', row['cc'] or '']) if addr}
            if target not in addresses:
                bad_ids.append(row['id'])
        for did in bad_ids:
            db.execute('DELETE FROM draft_items WHERE id=?',(did,))
    return len(bad_ids)


# Failed IMAP items stay in a durable retry queue, independent from scan_state.
def mark_scan_failure(account, folder, validity, uid, role, error, notify=False):
    import time
    now=time.time()
    with connect() as db:
        row=db.execute('''SELECT attempts,notify_on_recovery FROM scan_failures
            WHERE account=? AND folder=? AND uidvalidity=? AND uid=?''',
            (account,folder,str(validity),int(uid))).fetchone()
        attempts=int(row['attempts'])+1 if row else 1
        eligible=bool(notify or (row and row['notify_on_recovery']))
        # 1 min, 2 min, 4 min, ... max 24h; retry failures forever, but bounded per scan.
        delay=min(86400, 60*2**min(attempts-1,11))
        db.execute('''INSERT INTO scan_failures
            (account,folder,uidvalidity,uid,role,notify_on_recovery,attempts,error,next_retry_at,last_attempt_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account,folder,uidvalidity,uid) DO UPDATE SET
            attempts=excluded.attempts,error=excluded.error,
            next_retry_at=excluded.next_retry_at,last_attempt_at=excluded.last_attempt_at,
            notify_on_recovery=excluded.notify_on_recovery''',
            (account,folder,str(validity),int(uid),role,int(eligible),attempts,
             str(error)[:400],now+delay,now))


def clear_scan_failure(account,folder,validity,uid):
    with connect() as db:
        db.execute('DELETE FROM scan_failures WHERE account=? AND folder=? AND uidvalidity=? AND uid=?',
                   (account,folder,str(validity),int(uid)))


def stale_scan_failures(account,folder,validity):
    with connect() as db:
        db.execute('DELETE FROM scan_failures WHERE account=? AND folder=? AND uidvalidity<>?',
                   (account,folder,str(validity)))


def failed_scan_items(account,folder,validity,limit=20,force=False):
    import time
    with connect() as db:
        return [dict(r) for r in db.execute('''SELECT * FROM scan_failures
            WHERE account=? AND folder=? AND uidvalidity=? AND (? OR next_retry_at<=?)
            ORDER BY next_retry_at, uid LIMIT ?''',
            (account,folder,str(validity),int(force),time.time(),int(limit))).fetchall()]


def scan_failure_count(account,folder=None):
    with connect() as db:
        if folder is None:
            return db.execute('SELECT COUNT(*) FROM scan_failures WHERE account=?',(account,)).fetchone()[0]
        return db.execute('SELECT COUNT(*) FROM scan_failures WHERE account=? AND folder=?',
                          (account,folder)).fetchone()[0]


def enqueue_historical_gaps(account,folder,validity,server_uids,through_uid,role):
    """Register previously skipped UIDs (old cursor) for RAG-only repair.

    Only used when history limit is zero (whole-mailbox indexing). A prior
    failed initial fetch must never generate an old-mail notification.
    """
    if not through_uid or not server_uids:return 0
    now=__import__('time').time()
    with connect() as db:
        existing={r[0] for r in db.execute('''SELECT uid FROM messages WHERE account=? AND folder=? AND uidvalidity=?''',
                                         (account,folder,str(validity)))}
        queued={r[0] for r in db.execute('''SELECT uid FROM scan_failures WHERE account=? AND folder=? AND uidvalidity=?''',
                                       (account,folder,str(validity)))}
        missing=[int(uid) for uid in server_uids if int(uid)<=through_uid and uid not in existing and uid not in queued]
        db.executemany('''INSERT OR IGNORE INTO scan_failures
            (account,folder,uidvalidity,uid,role,notify_on_recovery,attempts,error,next_retry_at,last_attempt_at)
            VALUES(?,?,?,?,?,0,0,?,0,0)''',
            [(account,folder,str(validity),uid,role,'historical gap detected') for uid in missing])
        return len(missing)


def reference_page(account, subject, body, exclude_id, offset=0, page_size=40):
    """FTS5 retrieval using prior Korean bigram expansion; unlimited pagination.

    No fixed global result cap. Per-request page_size is only a performance bound.
    """
    offset=max(0,int(offset))
    page_size=max(1,min(100,int(page_size)))
    subject=re.sub(r'^(?:(?:re|fw|fwd)\s*:\s*)+', '', subject or '',flags=re.I)
    terms=list(dict.fromkeys(
        t for t in grams(subject)[:22]+grams((body or '')[:1500])[:24] if len(t)>1
    ))
    if not terms:
        return [],False
    query=' OR '.join('"'+t.replace('"','')+'"' for t in terms[:35])
    with connect() as db:
        rows=db.execute("""SELECT m.id,m.role,m.msgid,m.subject,m.sender,m.recipients,m.cc,
                    m.date_text,m.body,m.folder,bm25(message_search) AS search_score
                    FROM message_search JOIN messages m ON m.id=message_search.rowid
                    WHERE message_search MATCH ? AND m.account=? AND m.id<>?
                    ORDER BY search_score ASC, m.id DESC LIMIT ? OFFSET ?""",
                    (query,account,int(exclude_id),page_size+1,offset)).fetchall()
    return [dict(row) for row in rows[:page_size]],len(rows)>page_size

def get_reference_rows(account, ids):
    with connect() as db:
        return _source_rows(db, account, ids)


def operational_diagnostics(account):
    # Read-only operational snapshot. No passwords or message bodies returned.
    with connect() as db:
        message=db.execute('''SELECT COUNT(*) AS total,
            SUM(CASE WHEN length(trim(coalesce(body,'')))>0 THEN 1 ELSE 0 END) AS with_body,
            SUM(CASE WHEN role='inbox' THEN 1 ELSE 0 END) AS inbox,
            SUM(CASE WHEN role='sent' THEN 1 ELSE 0 END) AS sent
            FROM messages WHERE account=?''',(account,)).fetchone()
        folders=[dict(r) for r in db.execute('''SELECT folder,role,COUNT(*) AS total,
            SUM(CASE WHEN length(trim(coalesce(body,'')))>0 THEN 1 ELSE 0 END) AS with_body
            FROM messages WHERE account=? GROUP BY folder,role ORDER BY total DESC''',(account,))]
        failures=[dict(r) for r in db.execute('''SELECT folder,uid,attempts,
            next_retry_at,error FROM scan_failures WHERE account=?
            ORDER BY last_attempt_at DESC LIMIT 15''',(account,))]
        states=[dict(r) for r in db.execute('''SELECT folder,highest_uid FROM scan_state
            WHERE account=? ORDER BY folder''',(account,))]
    total=int(message['total'] or 0)
    with_body=int(message['with_body'] or 0)
    return {'indexed_total':total,'with_body':with_body,'header_only':total-with_body,
            'body_coverage_pct':round(100*with_body/total,1) if total else None,
            'inbox':int(message['inbox'] or 0),'sent':int(message['sent'] or 0),
            'folders':folders,'scan_cursors':states,'retry_pending':scan_failure_count(account),
            'recent_failures':failures,
            'note':'Indexed counts do not prove server-side completeness; compare to mailbox totals.'}


def save_reply_suggestion(account, message_id, result):
    """Per-mailbox suggestion. No auto-send and no storage of new credentials."""
    with connect() as db:
        valid = db.execute('SELECT 1 FROM messages WHERE id=? AND account=?', (message_id, account)).fetchone()
        if not valid:
            raise ValueError('Message does not belong to active mailbox')
        db.execute("""INSERT INTO reply_suggestions(message_id,account,model,draft,cited_ids,status,error_text,updated_at)
              VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
              ON CONFLICT(message_id) DO UPDATE SET
              account=excluded.account,model=excluded.model,draft=excluded.draft,
              cited_ids=excluded.cited_ids,status=excluded.status,
              error_text=excluded.error_text,updated_at=CURRENT_TIMESTAMP""",
              (message_id,account,result.get('model',''),result.get('draft',''),
               json.dumps(result.get('cited_ids',[])),result.get('status','error'),result.get('error','')))


def get_reply_suggestion(account, message_id):
    with connect() as db:
        row=db.execute('SELECT * FROM reply_suggestions WHERE account=? AND message_id=?',
                       (account,message_id)).fetchone()
    if not row: return None
    result=dict(row)
    try:result['cited_ids']=[int(i) for i in json.loads(result['cited_ids'])]
    except (ValueError,TypeError):result['cited_ids']=[]
    return result


def reference_search_count(account, subject, body, exclude_id):
    """Count all lexical matches across indexed account mails (not number displayed)."""
    subject=re.sub(r'^(?:(?:re|fw|fwd)\s*:\s*)+', '', subject or '',flags=re.I)
    terms=list(dict.fromkeys(t for t in grams(subject)[:22]+grams((body or '')[:1500])[:24] if len(t)>1))
    if not terms:return 0
    query=' OR '.join('"'+t.replace('"','')+'"' for t in terms[:35])
    with connect() as db:
        row=db.execute("""SELECT COUNT(*) AS n FROM message_search
          JOIN messages m ON m.id=message_search.rowid
          WHERE message_search MATCH ? AND m.account=? AND m.id<>?""",
          (query,account,int(exclude_id))).fetchone()
    return int(row['n'])


def save_mail_summary(account, message_id, model, body_hash, result):
    # Store only successful four-field AI summaries for an owned message.
    if result.get('status') != 'success':
        return False
    with connect() as db:
        owned=db.execute('SELECT 1 FROM messages WHERE id=? AND account=?', (message_id,account)).fetchone()
        if not owned: raise ValueError('Message does not belong to account')
        db.execute('INSERT INTO ai_mail_summaries(message_id, account, model, body_hash, summary_json, updated_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(message_id) DO UPDATE SET account=excluded.account,model=excluded.model,body_hash=excluded.body_hash,summary_json=excluded.summary_json,updated_at=CURRENT_TIMESTAMP',
            (message_id,account,model,body_hash,json.dumps(result,ensure_ascii=False)))
    return True


def get_mail_summary(account, message_id, model, body_hash):
    with connect() as db:
        row=db.execute('SELECT summary_json FROM ai_mail_summaries WHERE account=? AND message_id=? AND model=? AND body_hash=?',
          (account,message_id,model,body_hash)).fetchone()
    if not row: return None
    try: return json.loads(row['summary_json'])
    except (ValueError,TypeError): return None


# User-approved feedback is used as a retrieval preference, NOT as a model fine-tune.
def save_reply_feedback(account, message_id, rating, approved_text=''):
    if rating not in ('good','bad'): raise ValueError('Invalid rating')
    with connect() as db:
        owner=db.execute('SELECT 1 FROM messages WHERE account=? AND id=?',(account,int(message_id))).fetchone()
        reply=db.execute('SELECT draft,status FROM reply_suggestions WHERE account=? AND message_id=?',(account,int(message_id))).fetchone()
        if not owner or not reply or reply['status']!='success': raise ValueError('Reply not available for this account')
        text=reply['draft'] if rating=='good' else ''
        text=text.strip()[:9000]
        db.execute('''INSERT INTO reply_feedback(account,message_id,rating,approved_text)
            VALUES(?,?,?,?) ON CONFLICT(account,message_id) DO UPDATE SET
            rating=excluded.rating,approved_text=excluded.approved_text,updated_at=CURRENT_TIMESTAMP''',
            (account,int(message_id),rating,text))
    return {'rating':rating,'approved_text':text}


def get_reply_feedback(account, message_id):
    with connect() as db:
        row=db.execute('SELECT rating,approved_text FROM reply_feedback WHERE account=? AND message_id=?',
                       (account,int(message_id))).fetchone()
    return dict(row) if row else {'rating':None,'approved_text':''}


def save_evidence_feedback(account,message_id,reference_id,rating):
    if rating not in ('good','bad'):raise ValueError('Invalid rating')
    with connect() as db:
        owner=db.execute('SELECT 1 FROM messages WHERE account=? AND id=?',(account,int(message_id))).fetchone()
        ref=db.execute('SELECT 1 FROM messages WHERE account=? AND id=?',(account,int(reference_id))).fetchone()
        if not owner or not ref or int(message_id)==int(reference_id):raise ValueError('Reference not available for account')
        db.execute('''INSERT INTO evidence_feedback(account,message_id,reference_id,rating)
            VALUES(?,?,?,?) ON CONFLICT(account,message_id,reference_id) DO UPDATE SET
            rating=excluded.rating,updated_at=CURRENT_TIMESTAMP''',
            (account,int(message_id),int(reference_id),rating))
    return {'rating':rating}


def evidence_feedback_map(account,message_id,reference_ids):
    ids=list(dict.fromkeys(int(i) for i in reference_ids))[:200]
    if not ids:return {}
    with connect() as db:
        rows=db.execute('SELECT reference_id,rating FROM evidence_feedback WHERE account=? AND message_id=? AND reference_id IN ('+
            ','.join('?' for _ in ids)+')',[account,int(message_id),*ids]).fetchall()
    return {int(r['reference_id']):r['rating'] for r in rows}


def reference_preference(account,reference_ids):
    ids=list(dict.fromkeys(int(i) for i in reference_ids))[:200]
    if not ids:return {}
    with connect() as db:
        rows=db.execute('''SELECT reference_id,
             SUM(CASE WHEN rating='good' THEN 1 ELSE -1 END) AS score
             FROM evidence_feedback WHERE account=? AND reference_id IN ('''+','.join('?' for _ in ids)+
             ''') GROUP BY reference_id''',[account,*ids]).fetchall()
    return {int(r['reference_id']):int(r['score']) for r in rows}


def save_reply_style(account,values):
    greeting=str(values.get('greeting') or '').strip()[:120]
    closing=str(values.get('closing') or '').strip()[:120]
    length=values.get('length','normal')
    if length not in ('short','normal','detailed'):raise ValueError('Invalid length')
    phrases=str(values.get('preferred_phrases') or '').strip()[:1200]
    with connect() as db:
        db.execute('''INSERT INTO reply_style(account,greeting,closing,length,preferred_phrases)
           VALUES(?,?,?,?,?) ON CONFLICT(account) DO UPDATE SET
           greeting=excluded.greeting,closing=excluded.closing,length=excluded.length,
           preferred_phrases=excluded.preferred_phrases,updated_at=CURRENT_TIMESTAMP''',
           (account,greeting,closing,length,phrases))
    return get_reply_style(account)


def get_reply_style(account):
    with connect() as db:
        row=db.execute('SELECT greeting,closing,length,preferred_phrases FROM reply_style WHERE account=?',(account,)).fetchone()
    return dict(row) if row else {'greeting':'','closing':'','length':'normal','preferred_phrases':''}


def approved_reply_examples(account,subject,body,exclude_id,limit=3):
    """Prior approved replies from this login, ranked by matching keywords."""
    query_terms=set(keyword_terms(subject)) | set(keyword_terms((body or '')[:800]))
    with connect() as db:
        rows=db.execute('''SELECT m.id,m.subject,m.body,f.approved_text
          FROM reply_feedback f JOIN messages m ON m.id=f.message_id
          WHERE f.account=? AND m.account=? AND f.rating='good' AND m.id<>?
          ORDER BY f.updated_at DESC LIMIT 500''',(account,account,int(exclude_id))).fetchall()
    matches=[]
    for r in rows:
        subject_match=len(query_terms.intersection(keyword_terms(r['subject'])))
        body_match=len(query_terms.intersection(keyword_terms((r['body'] or '')[:900])))
        score=subject_match*3+body_match
        if score>0:
            matches.append((score,dict(r)))
    matches.sort(key=lambda x:(-x[0],-x[1]['id']))
    return [r for _,r in matches[:max(0,min(5,int(limit)))]]


def learned_reply_style(account):
    # Read-only observed characteristics of approved reply text (not model training).
    from collections import Counter
    with connect() as db:
        rows=db.execute('''SELECT approved_text FROM reply_feedback
           WHERE account=? AND rating='good' AND length(trim(approved_text))>0
           ORDER BY updated_at DESC LIMIT 200''',(account,)).fetchall()
    greetings=Counter(); closings=Counter(); lengths=[]; repeated=Counter()
    for row in rows:
        lines=[x.strip() for x in row['approved_text'].splitlines() if x.strip() and not x.strip().startswith('[근거:')]
        if not lines: continue
        lengths.append(len(row['approved_text']))
        if len(lines[0])<=75: greetings[lines[0]]+=1
        if len(lines)>1 and len(lines[-1])<=75: closings[lines[-1]]+=1
        for line in set(lines[1:-1]):
            if 8<=len(line)<=90:repeated[line]+=1
    count=len(lengths)
    avg=round(sum(lengths)/count) if count else 0
    return {'approved_count':count,'average_chars':avg,
            'common_greeting':greetings.most_common(1)[0][0] if greetings else '',
            'common_closing':closings.most_common(1)[0][0] if closings else '',
            'common_phrases':[k for k,v in repeated.most_common(5) if v>=2]}



def _normal_subject(value):
    """Strip reply prefixes; this is only a fallback signal, never an RFC thread ID."""
    return re.sub(r'^(?:(?:re|fw|fwd|답장|전달)\s*:\s*)+', '', value or '', flags=re.I).strip().casefold()


def _rfc_ids(value):
    return {s.casefold() for s in re.findall(r'<[^<>\s]{3,320}>', value or '')}


def historical_reply_pairs(account, subject, body, exclude_id, limit=5):
    """Find historically received email + actual sent reply pairs.

    Reliable: sent In-Reply-To or References contains the inbound Message-ID.
    Fallback: SAME normalized subject, matching reply-to address and reasonable date
    ordering, clearly labeled 'subject_match' (never claim RFC thread verified).
    """
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime, getaddresses
    def stamp(text):
        try:
            dt=parsedate_to_datetime(text or '')
            if not dt:return None
            if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (TypeError,ValueError,OverflowError):return None
    def addresses(value):
        return {email.casefold() for _,email in getaddresses([value or '']) if '@' in email}
    from itertools import chain
    # Search first; avoid pairing arbitrary unrelated mailbox traffic.
    base=history_retrieve(account,subject,body,exclude_id,limit=150)
    mail_ids=[int(r['id']) for r in base]
    if not mail_ids:return []
    marks=','.join('?' for _ in mail_ids)
    with connect() as db:
        candidates=[dict(r) for r in db.execute(
           f"""SELECT id,account,role,subject,sender,recipients,cc,date_text,body,msgid,
                      in_reply_to,references_header,folder FROM messages
                WHERE account=? AND id IN ({marks})""",[account,*mail_ids]).fetchall()]
        # Include the receiving originals referenced by a candidate's RFC headers,
        # even if the originals did not match FTS query terms.
        sent=[r for r in candidates if r['role']=='sent']
        refs=set(chain.from_iterable(_rfc_ids(r.get('in_reply_to')) | _rfc_ids(r.get('references_header')) for r in sent))
        if refs:
            # Equality by normalized message-id after loading account-scoped inbox rows.
            inbound=[dict(r) for r in db.execute("""SELECT id,account,role,subject,sender,recipients,cc,date_text,body,msgid,
                    in_reply_to,references_header,folder FROM messages
                    WHERE account=? AND role='inbox' AND msgid IS NOT NULL AND msgid<>'' """,(account,)).fetchall()]
            prior={r['id'] for r in candidates}
            candidates.extend(r for r in inbound if _rfc_ids(r.get('msgid')) & refs and r['id'] not in prior)
    inbound=[r for r in candidates if r['role']=='inbox']
    sent=[r for r in candidates if r['role']=='sent']
    query_terms=set(grams(_normal_subject(subject)))
    pairs=[]
    for out in sent:
        ref_ids=_rfc_ids(out.get('in_reply_to')) | _rfc_ids(out.get('references_header'))
        matches=[]
        for inc in inbound:
            if int(inc['id'])==int(out['id']):continue
            actual=bool(_rfc_ids(inc.get('msgid')) & ref_ids)
            if actual:
                relation='rfc_thread'
            else:
                # Conservative inferred pairing, never assert the emails were replies.
                if not _normal_subject(inc['subject']) or _normal_subject(inc['subject'])!=_normal_subject(out['subject']):continue
                if not (addresses(inc.get('sender')) & addresses(out.get('recipients'))):continue
                it,ot=stamp(inc.get('date_text')),stamp(out.get('date_text'))
                if it is None or ot is None or not (0<=ot-it<=30*86400):continue
                relation='subject_match'
            sim=len(query_terms & set(grams(_normal_subject(inc.get('subject')))))
            # Prefer pairs matching current topic, with confirmed relation first.
            score=(100 if relation=='rfc_thread' else 20)+sim*10
            matches.append((score,inc,relation))
        if matches:
            score,inc,relation=max(matches,key=lambda x:x[0]);pairs.append({'inbound':inc,'outbound':out,'relation':relation,'score':score})
    pairs.sort(key=lambda p:p['score'],reverse=True)
    seen=set();result=[]
    for pair in pairs:
        key=(pair['inbound']['id'],pair['outbound']['id'])
        if key not in seen:seen.add(key);result.append(pair)
        if len(result)>=limit:break
    return result

def preferred_outbound_refs(account,subject,body,exclude_id,limit=10):
    # Prioritize actual sent messages without changing account scoping.
    # Fetch more initial candidates and retain the most relevant outgoing ones.
    return [r for r in history_retrieve(account,subject,body,exclude_id,limit=100)
            if r.get('role')=='sent'][:max(0,min(20,int(limit)))]
