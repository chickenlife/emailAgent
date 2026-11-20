"""My AI Secretary - Mail Edition. Restricted remote-access FastAPI app."""
import argparse
import hashlib
import queue
from account_queue import AccountFairQueue
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import quote

from dotenv import load_dotenv
BASE=Path(__file__).resolve().parent
load_dotenv(BASE/'.env')

import credentials
import storage
import mailcore
import mail_summary
import reply_ai

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

PORT=int(os.getenv('WEB_PORT','8765'))
WEB_BIND_HOST=os.getenv('WEB_BIND_HOST','192.168.25.110').strip()
WEB_PUBLIC_HOST=os.getenv('WEB_PUBLIC_HOST',WEB_BIND_HOST).strip()
WEB_TLS_CERT=os.getenv('WEB_TLS_CERT','').strip()
WEB_TLS_KEY=os.getenv('WEB_TLS_KEY','').strip()
USE_HTTPS=bool(WEB_TLS_CERT and WEB_TLS_KEY)
ALLOW_INSECURE_HTTP=os.getenv('ALLOW_INSECURE_WEB_HTTP','false').lower()=='true'
POLL_SECONDS=max(30,int(os.getenv('POLL_SECONDS','300')))
BASE_URL=f'{"https" if USE_HTTPS else "http"}://{WEB_PUBLIC_HOST}:{PORT}'
# Ephemeral sessions are invalidated when the process restarts.
# Each login is validated against IMAP before a cookie is issued.
SESSIONS={}
SESSION_LOCK=threading.Lock()
SESSION_SECONDS=max(300,int(os.getenv('WEB_SESSION_SECONDS','28800')))
COOKIE_SECURE=USE_HTTPS

logger=logging.getLogger('secretary')
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout),
                    logging.FileHandler(BASE/'mailagent.log',encoding='utf-8')])
app=FastAPI(title='나만의 AI비서 - 메일편',docs_url=None,redoc_url=None,openapi_url=None)
app.mount('/static',StaticFiles(directory=str(BASE/'static')),name='static')

def _session(request):
    key=request.cookies.get('mailagent_session','')
    if not key:return None
    with SESSION_LOCK:
        session=SESSIONS.get(key)
        if not session:return None
        if session['expires']<time.time():
            SESSIONS.pop(key,None)
            return None
        session['expires']=time.time()+SESSION_SECONDS
        return dict(session)


def _require_session(request):
    session=_session(request)
    if not session:raise HTTPException(401,'Login required')
    active=credentials.load(session['username'])
    if not active or active.get('username')!=session['username']:
        raise HTTPException(401,'Session is no longer valid')
    return session


def _check_origin(request):
    origin=request.headers.get('origin','')
    if origin and origin not in (BASE_URL, f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'):
        raise HTTPException(403,'Invalid origin')
    # Prevent cross-site form submissions: same-origin application API.
    if request.method not in ('GET','HEAD','OPTIONS'):
        site=request.headers.get('sec-fetch-site','')
        if site and site not in ('same-origin','none'):
            raise HTTPException(403,'Cross-site request blocked')


@app.middleware('http')
async def restrict_remote_access(request: Request, call_next):
    host=request.headers.get('host','').lower()
    allowed={f'127.0.0.1:{PORT}',f'localhost:{PORT}',
             f'{WEB_BIND_HOST.lower()}:{PORT}',f'{WEB_PUBLIC_HOST.lower()}:{PORT}'}
    if host not in allowed:
        return Response('Unknown Host',status_code=403)
    # Disallow accidental caching of private mail pages.
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    return response

STATUS_TEMPLATE={'last_check':None,'last_error':None,'last_scan':None,'busy':False,
        'phase':'idle','folder':None,'processed':0,'total':0,'failed':0,'scan_started':None,
        'last_activity':None,'active_uid':None,'retry_pending':0,'retrying':False,
        'folder_warnings':[],'scan_outcome':'not_checked','history_remaining':0}
ACCOUNT_STATUS={}
ACCOUNT_STATUS_LOCK=threading.RLock()

def _status_for(account):
    with ACCOUNT_STATUS_LOCK:
        return ACCOUNT_STATUS.setdefault(account, {**STATUS_TEMPLATE, 'folder_warnings': []})

def _active_accounts():
    now=time.time()
    with SESSION_LOCK:
        stale=[sid for sid,s in SESSIONS.items() if s['expires']<now]
        for sid in stale: SESSIONS.pop(sid,None)
        return sorted({s['username'] for s in SESSIONS.values()})

scan_lock=threading.Lock()
stop_event=threading.Event()
AI_QUEUE=AccountFairQueue()
AI_PENDING=set()
AI_PENDING_LOCK=threading.Lock()
AI_TASK_LOCK=threading.Lock()  # Serialize Ollama calls on 6 GB VRAM
AI_WORKER_STARTED=False
REFERENCE_AI_QUEUE=AccountFairQueue()
REFERENCE_AI_PENDING=set()
REFERENCE_AI_PENDING_LOCK=threading.Lock()
REFERENCE_AI_WORKER_STARTED=False
REFERENCE_AI_PREFETCH_LIMIT=max(0,min(20,int(os.getenv('REFERENCE_AI_PREFETCH_LIMIT','10'))))

def _reply_model():
    return reply_ai.configured_model()

def _summary_model():
    return (os.getenv('MAIL_SUMMARY_MODEL') or os.getenv('OLLAMA_MODEL') or 'qwen3:1.7b').strip()

def _body_hash(item):
    payload=(str(item.get('subject') or '')+'\0'+str(item.get('body') or ''))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()

def _summary_for_mail(account, item):
    model=_summary_model()
    mid=int(item['message_id'])
    cached=storage.get_mail_summary(account,mid,model,_body_hash(item))
    if cached:return cached
    with AI_TASK_LOCK:
        cached=storage.get_mail_summary(account,mid,model,_body_hash(item))
        if cached:return cached
        result=mail_summary.summary_for_display(item.get('subject',''),item.get('body',''))
        storage.save_mail_summary(account,mid,model,_body_hash(item),result)
        return result

def _reply_for_mail(account, item):
    existing=storage.get_reply_suggestion(account,item['message_id'])
    if existing and existing.get('status')=='success' and existing.get('model')==_reply_model(): return existing
    with AI_TASK_LOCK:
        existing=storage.get_reply_suggestion(account,item['message_id'])
        if existing and existing.get('status')=='success' and existing.get('model')==_reply_model(): return existing
        sources=[]
        if os.getenv('RAG_VECTOR_ENABLED','false').lower()=='true':
            try:
                import rag_hybrid
                sources=rag_hybrid.retrieve(account,item['subject'],item['body'],item['message_id'],limit=12)
            except Exception as exc: logger.warning('Reply hybrid RAG unavailable: %s',exc)
        if not sources:
            sources=storage.history_retrieve(account,item['subject'],item['body'],item['message_id'],limit=12)
        if len(sources)<12:
            seen={int(r['id']) for r in sources}
            for row in storage.history_retrieve(account,item['subject'],item['body'],item['message_id'],limit=20):
                if int(row['id']) not in seen:
                    sources.append(row);seen.add(int(row['id']))
                if len(sources)>=12:break
        # Prioritize real historical inbound->outbound reply threads over unpaired mail.
        pairs=storage.historical_reply_pairs(account,item['subject'],item['body'],item['message_id'],limit=5)
        # Expand relevant sent-mail context, since these show the user's actual reply tone.
        existing_ids={int(r['id']) for r in sources}
        for candidate in storage.preferred_outbound_refs(account,item['subject'],item['body'],item['message_id'],limit=10):
            if int(candidate['id']) not in existing_ids:
                sources.append(candidate);existing_ids.add(int(candidate['id']))
        # Promote outbound mail and prior positively-rated citations.
        preference=storage.reference_preference(account,[r['id'] for r in sources])
        sources.sort(key=lambda r:(preference.get(int(r['id']),0),
                                   1 if r.get('role')=='sent' else 0,
                                   float(r.get('rank',0) or 0)),reverse=True)
        # Actual sent responses of selected historical pairs are the highest-priority evidence.
        pair_out={int(p['outbound']['id']) for p in pairs}
        selected=[p['outbound'] for p in pairs if preference.get(int(p['outbound']['id']),0)>=0]
        selected_ids={int(r['id']) for r in selected}
        for r in sources:
            if int(r['id']) not in selected_ids and preference.get(int(r['id']),0)>=0:
                selected.append(r);selected_ids.add(int(r['id']))
        sources=selected[:12]
        # Only pass paired examples where outgoing evidence is actually in the model prompt.
        pairs=[p for p in pairs if int(p['outbound']['id']) in selected_ids][:5]
        style=storage.get_reply_style(account)
        observed=storage.learned_reply_style(account)
        if not style['greeting']:style['greeting']=observed['common_greeting']
        if not style['closing']:style['closing']=observed['common_closing']
        if not style['preferred_phrases']:style['preferred_phrases']='; '.join(observed['common_phrases'])
        examples=storage.approved_reply_examples(account,item['subject'],item['body'],item['message_id'])
        logger.info('Reply generation: mid=%s model=%s sent_refs=%s paired_examples=%s', item['message_id'],_reply_model(),sum(r.get('role')=='sent' for r in sources),len(pairs))
        result=reply_ai.generate(item['subject'],item['body'],sources,style=style,examples=examples,pairs=pairs)
        storage.save_reply_suggestion(account,item['message_id'],result)
        return result


def enqueue_reference_ai(account, ids):
    """Low-priority, account-scoped reference summary requests (deduplicated)."""
    for mid in ids:
        key=(account,int(mid))
        with REFERENCE_AI_PENDING_LOCK:
            if key in REFERENCE_AI_PENDING:continue
            REFERENCE_AI_PENDING.add(key)
        REFERENCE_AI_QUEUE.put(key)

def prefetch_reference_ai(account,item,reply):
    """Prepare cited references and top matches before the user opens the mail."""
    if not os.getenv('MAIL_SUMMARY_AI','true').lower()=='true':return
    if REFERENCE_AI_PREFETCH_LIMIT==0:return
    mid=int(item['message_id'])
    cited=reply.get('cited_ids',[]) if reply.get('status')=='success' else []
    related,_=storage.reference_page(account,item.get('subject',''),item.get('body',''),mid,0,REFERENCE_AI_PREFETCH_LIMIT+len(cited))
    ordered=[];seen=set()
    for rid in list(cited)+[r['id'] for r in related]:
        rid=int(rid)
        if rid!=mid and rid not in seen:
            ordered.append(rid);seen.add(rid)
    enqueue_reference_ai(account,ordered[:REFERENCE_AI_PREFETCH_LIMIT+len(cited)])

def reference_ai_worker():
    """One background worker; incoming-mail AI always has priority."""
    while not stop_event.is_set():
        try:account,mid=REFERENCE_AI_QUEUE.get(timeout=1)
        except queue.Empty:continue
        try:
            current=credentials.load(account)
            if not current:continue
            # Defer to waiting new-mail work rather than contending for the GPU.
            while not stop_event.is_set() and not AI_QUEUE.empty():
                stop_event.wait(1)
            rows=storage.get_reference_rows(account,[mid])
            if not rows or not str(rows[0].get('body') or '').strip():continue
            result=_summary_for_mail(account,{**rows[0],'message_id':mid})
            logger.info('Background reference summary: mail_id=%s status=%s model=%s',mid,result.get('status'),result.get('model'))
        except Exception:
            logger.exception('Background reference summary failed: mail_id=%s',mid)
        finally:
            with REFERENCE_AI_PENDING_LOCK:REFERENCE_AI_PENDING.discard((account,mid))
            REFERENCE_AI_QUEUE.task_done()


def enqueue_new_mail_ai(account, did):
    # Schedule newly registered Inbox mail; IMAP polling is never blocked by AI.
    key=(account,int(did))
    with AI_PENDING_LOCK:
        if key in AI_PENDING:return
        AI_PENDING.add(key)
    AI_QUEUE.put(key)

def ai_mail_worker():
    while not stop_event.is_set():
        try: account,did=AI_QUEUE.get(timeout=1)
        except queue.Empty:continue
        try:
            current_cred=credentials.load(account)
            if not current_cred:
                logger.info('Background AI skipped: mailbox account changed')
                continue
            item=storage.get_draft(did,account)
            if not item:continue  # Deleted or no longer in list.
            if not str(item.get('body') or '').strip():
                logger.info('Background AI skipped: no text body, item=%s',did)
                continue
            summary=_summary_for_mail(account,item)
            logger.info('Background summary: item=%s status=%s model=%s',did,summary.get('status'),summary.get('model'))
            # Even if summarization fails, attempt the independent grounded draft.
            reply=_reply_for_mail(account,item)
            logger.info('Background reply: item=%s status=%s citations=%s',did,reply.get('status'),len(reply.get('cited_ids',[])))
            prefetch_reference_ai(account,item,reply)
        except Exception:
            logger.exception('Background AI failed for item=%s',did)
        finally:
            with AI_PENDING_LOCK:AI_PENDING.discard((account,did))
            AI_QUEUE.task_done()



class CredentialsIn(BaseModel):
    username:str
    password:str


def require_token(request:Request):
    # Backward-compatible call sites use session-based access control.
    _check_origin(request)
    return _require_session(request)


@app.post('/api/login')
def login(data:CredentialsIn,request:Request):
    _check_origin(request)
    username=data.username.strip()
    if not username or not data.password or len(username)>320 or len(data.password)>1000:
        raise HTTPException(400,'Invalid credentials')
    # Authenticate with IMAP before persisting an independent encrypted credential.
    try:
        mailcore.check_connection(username,data.password)
    except Exception as exc:
        logger.warning('IMAP authentication failed for login')
        raise HTTPException(401,'IMAP account authentication failed') from exc
    # Mailboxes coexist: never clear other users' sessions or credentials.
    credentials.save(username,data.password)
    sid=secrets.token_urlsafe(32)
    with SESSION_LOCK:
        SESSIONS[sid]={'username':username,'expires':time.time()+SESSION_SECONDS}
    # A new account is scheduled for its own initial index without preempting
    # an ongoing account scan. The global IMAP scan lock protects server load.
    threading.Thread(target=do_scan,args=(username,),daemon=True).start()
    response=JSONResponse({'ok':True,'username':username})
    response.set_cookie('mailagent_session',sid,httponly=True,secure=COOKIE_SECURE,
                        samesite='strict',max_age=SESSION_SECONDS,path='/')
    return response


@app.post('/api/logout')
def logout(request:Request):
    require_token(request)
    sid=request.cookies.get('mailagent_session','')
    with SESSION_LOCK:SESSIONS.pop(sid,None)
    response=JSONResponse({'ok':True})
    response.delete_cookie('mailagent_session',path='/')
    return response


@app.get('/api/session')
def session_status(request:Request):
    session=_session(request)
    valid=bool(session and credentials.load(session['username']))
    return {'authenticated':valid,'username':session['username'] if valid else None}

def process_message(account, mid, notify=True):
    """Register incoming mail for keyword reference search; never call an LLM."""
    with storage.connect() as db:
        m=db.execute('SELECT role FROM messages WHERE id=? AND account=?',(mid,account)).fetchone()
    if not m or m['role'] != 'inbox':
        return None
    with storage.connect() as db:
        existed=db.execute('SELECT id FROM draft_items WHERE message_id=?',(mid,)).fetchone()
    did=storage.create_draft(mid)
    if not existed:
        enqueue_new_mail_ai(account, did)
    if notify and not existed:
        try:
            from winotify import Notification
            row=storage.get_draft(did, account)
            Notification(app_id='Mail RAG',title='새 메일 수집',
                         msg=(row['subject'] if row else '새 메일')[:170],
                         duration='short',launch=f'{BASE_URL}/?mail={did}').show()
        except Exception as exc:
            logger.warning('Mail notification unavailable: %s',exc)
    return did

# Separate polling path prevents long RAG history indexing from starving
# notifications for recently delivered inbox messages.
priority_lock = threading.Lock()
draft_lock = threading.RLock()
reply_lock = threading.Lock()

def do_priority_scan(account):
    if not priority_lock.acquire(blocking=False):
        return
    try:
        if account not in _active_accounts():return
        cred = credentials.load(account)
        if not cred:return
        ids = mailcore.scan_priority_inbox(cred['username'], cred['password'])
        if ids:
            logger.info('Priority inbox: %s new received messages', len(ids))
        for mid in ids:
            try:
                with draft_lock:
                    process_message(cred['username'], mid)
            except Exception:
                logger.exception('Priority mail registration failed for mid=%s', mid)
    except Exception:
        logger.exception('Priority inbox scan failed')
    finally:
        priority_lock.release()


def do_scan(account,force_retry=False):
    if not scan_lock.acquire(blocking=False):return
    STATUS=_status_for(account)
    STATUS.update(busy=True,phase='connecting',folder=None,processed=0,total=0,failed=0,
                  scan_started=time.strftime('%Y-%m-%d %H:%M:%S'),last_activity=time.time(),
                  active_uid=None,retry_pending=0,retrying=False,folder_warnings=[],scan_outcome='running')
    try:
        if account not in _active_accounts():return
        cred=credentials.load(account)
        if not cred:return
        def update_scan_progress(folder,role,processed,total,failed,uid=None,retrying=False,retry_pending=0):
            STATUS.update(phase='retrying' if retrying else 'indexing',folder=folder,
                          processed=processed,total=total,failed=failed,
                          active_uid=uid,last_activity=time.time(),
                          retry_pending=retry_pending,retrying=retrying)
        # Register new incoming messages before scanning historical references.
        do_priority_scan(account)
        ids,result=mailcore.scan(cred['username'],cred['password'],progress=update_scan_progress,
                                 force_retry=force_retry)
        STATUS['last_scan']=result
        STATUS['history_remaining']=sum(int(i.get('history_remaining',0)) for i in result.get('folders',{}).values())
        warnings=result.get('warnings') or []
        STATUS['folder_warnings']=warnings
        STATUS.update(phase='indexing',folder='INBOX',processed=0,total=len(ids),failed=0,
                      last_activity=time.time(),active_uid=None,retrying=False)
        for index,mid in enumerate(ids,1):
            try:
                with draft_lock:
                    process_message(cred['username'],mid)
            except Exception as exc:
                STATUS['failed']+=1
                logger.exception('Incoming mail registration failed mid=%s: %s',mid,exc)
            finally:
                STATUS['processed']=index
                STATUS['last_activity']=time.time()
        STATUS['retry_pending']=storage.scan_failure_count(cred['username'])
        # An incomplete folder scan is NOT a clean completion.
        if warnings or result.get('inbox') is None or STATUS['retry_pending']:
            STATUS['last_error']=('; '.join(warnings[:3]) if warnings else
                'INBOX was not scanned' if result.get('inbox') is None else
                f"본문 수집 실패 재시도 대기 {STATUS['retry_pending']}건")
            STATUS['scan_outcome']='partial'
            STATUS['phase']='warning'
        else:
            STATUS['last_error']=None
            STATUS['scan_outcome']='complete'
            STATUS['phase']='complete'
        STATUS['last_check']=time.strftime('%Y-%m-%d %H:%M:%S')
        logger.info('Scan finished (outcome=%s): %s new, inbox %s, folder warnings %s',
                    STATUS['scan_outcome'],len(ids),result.get('inbox'),len(warnings))
    except Exception as e:
        STATUS['last_error']=f'{type(e).__name__}: {str(e)}'
        STATUS['phase']='error'
        STATUS['scan_outcome']='error'
        logger.exception('Mailbox scan failed')
    finally:
        STATUS['busy']=False
        STATUS['last_activity']=time.time()
        scan_lock.release()


def scheduler():
    # Round-robin between sessions; never share an IMAP username/password.
    # Slow history backfills cannot trigger concurrent full scans.
    last_full={}
    while not stop_event.wait(30):
        for account in _active_accounts():
            do_priority_scan(account)
        now=time.monotonic()
        for account in _active_accounts():
            if now-last_full.get(account,0)>=POLL_SECONDS and not scan_lock.locked():
                last_full[account]=now
                threading.Thread(target=do_scan,args=(account,),daemon=True).start()
                break


@app.on_event('startup')
def startup():
    storage.initialize()
    # No startup purge: registered INBOX messages are retained.
    global AI_WORKER_STARTED
    if not AI_WORKER_STARTED:
        threading.Thread(target=ai_mail_worker,daemon=True,name='mailagent-ai-worker').start()
        AI_WORKER_STARTED=True
    global REFERENCE_AI_WORKER_STARTED
    if not REFERENCE_AI_WORKER_STARTED:
        threading.Thread(target=reference_ai_worker,daemon=True,name='mailagent-reference-ai-worker').start()
        REFERENCE_AI_WORKER_STARTED=True
    threading.Thread(target=scheduler,daemon=True).start()
    # Mail scans start only after an interactive IMAP-backed web login.


@app.get('/',response_class=HTMLResponse)
def home():
    html=(BASE/'static'/'index.html').read_text(encoding='utf-8')
    return HTMLResponse(html)


@app.get('/api/status')
def status(request:Request):
    session=require_token(request)
    account=session['username']
    state=_status_for(account)
    elapsed=(time.time()-state['last_activity']) if state['busy'] and state['last_activity'] else 0
    return {'configured':True,'username':account,'poll_seconds':POLL_SECONDS,
            'idle_seconds':int(elapsed),'stalled':bool(state['busy'] and elapsed>90),**state}


@app.get('/api/rag-status')
def rag_status(request:Request):
    session=require_token(request)
    cred=credentials.load(session['username'])
    if not cred:raise HTTPException(401,'Not authenticated')
    return storage.rag_stats(cred['username'])


@app.get('/api/diagnostics')
def diagnostics(request:Request):
    session=require_token(request)
    cred=credentials.load(session['username'])
    if not cred:raise HTTPException(401,'Not authenticated')
    result=storage.operational_diagnostics(cred['username'])
    state=_status_for(cred['username'])
    result['scan_outcome']=state['scan_outcome']
    result['folder_warnings']=state['folder_warnings']
    result['last_check']=state['last_check']
    result['busy']=state['busy']
    return result


@app.get('/api/items')
def items(request:Request):
    session=require_token(request)
    cred=credentials.load(session['username'])
    return storage.list_drafts(cred['username']) if cred else []


@app.get('/api/items/{did}')
def detail(did:int,request:Request):
    session=require_token(request)
    cred=credentials.load(session['username'])
    item=storage.get_draft(did,cred['username']) if cred else None
    if not item:raise HTTPException(404,'Not found')
    item['body_display']=mail_summary.clean_body(item.get('body',''))
    item['summary_structured']=mail_summary.fallback_structured_summary(item.get('subject',''), item.get('body',''))
    cached_summary=storage.get_mail_summary(cred['username'],item['message_id'],_summary_model(),_body_hash(item))
    item['summary_status']='success' if cached_summary else ('loading' if item.get('body') else 'no_body')
    item['summary_model']=(os.getenv('MAIL_SUMMARY_MODEL') or os.getenv('OLLAMA_MODEL') or 'qwen3:1.7b')
    item['summary_status_label']='AI 분석 성공' if cached_summary else ('AI 분석 대기 · 추출 요약' if item.get('body') else '본문 없음 · 추출 요약')
    if cached_summary:
        item['summary_structured']=cached_summary['summary']
        item['summary_model']=cached_summary.get('model') or _summary_model()
    item['date_text']=mail_summary.format_date(item.get('date_text',''))
    reply=storage.get_reply_suggestion(cred['username'],item['message_id'])
    # A cached suggestion may belong to an older model; show its actual model until regenerated.
    item['reply_suggestion']=reply if not reply or reply.get('model')==_reply_model() else None
    item['reply_model_configured']=_reply_model()
    item['reply_feedback']=storage.get_reply_feedback(cred['username'],item['message_id'])
    return item


@app.get('/api/items/{did}/summary')
def detailed_summary(did:int,request:Request):
    session=require_token(request)
    cred=credentials.load(session['username'])
    item=storage.get_draft(did,cred['username']) if cred else None
    if not item:raise HTTPException(404,'Not found')
    return _summary_for_mail(cred['username'],item)


@app.get('/api/items/{did}/references')
def references(did:int,request:Request,offset:int=0,page_size:int=40,mode:str='cited'):
    """Default: model-cited mail only. All results available by explicit user choice."""
    session=require_token(request)
    cred=credentials.load(session['username'])
    account=cred['username'] if cred else None
    item=storage.get_draft(did,account) if account else None
    if not item:raise HTTPException(404,'Not found')
    offset=max(0,int(offset));page_size=max(1,min(100,int(page_size)))
    mid=int(item['message_id'])
    total=storage.reference_search_count(account,item.get('subject'),item.get('body'),mid)
    if mode=='cited':
        saved=storage.get_reply_suggestion(account,mid)
        if saved and saved.get('model')!=_reply_model():saved=None
        ids=saved.get('cited_ids',[]) if saved and saved.get('status')=='success' else []
        rows=storage.get_reference_rows(account,ids)
        related=rows[offset:offset+page_size]
        has_more=offset+page_size<len(rows)
        search_mode='cited'
    elif mode=='all':
        related,has_more=storage.reference_page(account,item['subject'],item['body'],mid,offset,page_size)
        search_mode='FTS5'
        if offset==0 and os.getenv('RAG_VECTOR_ENABLED','false').lower()=='true':
            try:
                import rag_hybrid
                semantic=rag_hybrid.retrieve(account,item['subject'],item['body'],mid,limit=40)
                # Keep semantic candidates visible, but never claim they are cited.
                used=set();combined=[]
                for candidate in semantic+related:
                    rid=int(candidate['id'])
                    if rid!=mid and rid not in used:used.add(rid);combined.append(candidate)
                related=combined
                search_mode='hybrid'
            except Exception as exc:
                logger.warning('Hybrid reference retrieval failed: %s',exc)
    else:
        raise HTTPException(400,'Unknown reference mode')
    feedback=storage.evidence_feedback_map(account,mid,[r['id'] for r in related])
    for candidate in related:
        candidate['feedback_rating']=feedback.get(int(candidate['id']))
        candidate['body_display']=mail_summary.clean_body(candidate.get('body',''))
        candidate['summary_structured']=mail_summary.fallback_structured_summary(candidate.get('subject',''),candidate.get('body',''))
        candidate['summary']=candidate['summary_structured']['main']
        candidate['date_text']=mail_summary.format_date(candidate.get('date_text',''))
    return {'results':related,'next_offset':offset+page_size,'has_more':has_more,
            'search_mode':search_mode,'search_total':total,'cited_total':len(ids) if mode=='cited' else None,
            'note':'Citations are model-selected labels. Verify factual claims against original mail.'}

@app.get('/api/items/{did}/reply')
def get_reply(did:int,request:Request):
    session=require_token(request)
    account=session['username']
    item=storage.get_draft(did,account)
    if not item:raise HTTPException(404,'Not found')
    return storage.get_reply_suggestion(account,item['message_id']) or {
        'draft':'','cited_ids':[],'status':'pending','model':os.getenv('MAIL_REPLY_MODEL') or os.getenv('MAIL_SUMMARY_MODEL') or 'qwen3.5:4b'}

@app.post('/api/items/{did}/reply')
def make_reply(did:int,request:Request):
    """Generate on demand, cache successful results. Never send an email."""
    session=require_token(request)
    account=session['username']
    item=storage.get_draft(did,account)
    if not item:raise HTTPException(404,'Not found')
    return _reply_for_mail(account,item)

class RatingIn(BaseModel):
    rating:str
    approved_text:str=''  # ignored: approved text is the model-produced draft

class StyleIn(BaseModel):
    greeting:str=''
    closing:str=''
    length:str='normal'
    preferred_phrases:str=''

@app.post('/api/items/{did}/feedback')
def feedback_reply(did:int,data:RatingIn,request:Request):
    session=require_token(request)
    account=session['username']
    item=storage.get_draft(did,account)
    if not item:raise HTTPException(404,'Not found')
    try:return storage.save_reply_feedback(account,item['message_id'],data.rating)
    except ValueError as exc:raise HTTPException(400,str(exc))

@app.post('/api/items/{did}/references/{mid}/feedback')
def feedback_reference(did:int,mid:int,data:RatingIn,request:Request):
    session=require_token(request)
    account=session['username']
    item=storage.get_draft(did,account)
    if not item:raise HTTPException(404,'Not found')
    try:return storage.save_evidence_feedback(account,item['message_id'],mid,data.rating)
    except ValueError as exc:raise HTTPException(400,str(exc))

@app.get('/api/reply-style')
def get_style(request:Request):
    session=require_token(request)
    account=session['username']
    return {**storage.get_reply_style(account),'observed':storage.learned_reply_style(account)}

@app.put('/api/reply-style')
def put_style(data:StyleIn,request:Request):
    session=require_token(request)
    try:return storage.save_reply_style(session['username'],data.model_dump() if hasattr(data,'model_dump') else data.dict())
    except ValueError as exc:raise HTTPException(400,str(exc))

@app.get('/api/items/{did}/references/{mid}/summary')
def reference_summary(did:int, mid:int, request:Request):
    """On-demand four-field summary for an account-owned reference, never all search results at once."""
    session=require_token(request)
    cred=credentials.load(session['username'])
    account=cred['username'] if cred else None
    item=storage.get_draft(did, account) if account else None
    if not item: raise HTTPException(404,'Not found')
    rows=storage.get_reference_rows(account,[mid])
    if not rows: raise HTTPException(404,'Reference not found')
    source=rows[0]
    # Return an existing account/model/body-specific summary if already available.
    # The shared lock prevents duplicate Ollama calls across background and web.
    return _summary_for_mail(account,{**source,'message_id':mid})


class BulkDeleteIn(BaseModel):
    ids:list[int]


@app.post('/api/items/bulk-delete')
def bulk_delete(data:BulkDeleteIn,request:Request):
    """One account-scoped transaction; never removes mail from IMAP."""
    session=require_token(request)
    cred=credentials.load(session['username'])
    ids=data.ids
    if not cred or not ids or len(ids)>200 or any(x<=0 for x in ids):
        raise HTTPException(400,'Select between 1 and 200 valid mail IDs')
    removed=storage.delete_drafts(cred['username'],ids)
    return {'ok':True,'deleted_ids':removed,'deleted_count':len(removed),
            'note':'Removed from processing list only; IMAP originals unchanged.'}


@app.delete('/api/items/{did}')
def delete(did:int,request:Request):
    session=require_token(request)
    cred=credentials.load(session['username'])
    if not cred or not storage.get_draft(did,cred['username']):raise HTTPException(404,'Not found')
    storage.delete_draft(did,cred['username'])
    return {'ok':True,'note':'Deleted from app list only; mail server unchanged.'}


@app.post('/api/scan')
def scan_now(request:Request):
    session=require_token(request)
    if scan_lock.locked():return {'ok':False,'reason':'A mailbox is already scanning'}
    threading.Thread(target=do_scan,args=(session['username'],),daemon=True).start()
    return {'ok':True}



@app.post('/api/retry-failed')
def retry_failed(request:Request):
    session=require_token(request)
    if scan_lock.locked():return {'ok':False,'reason':'A mailbox is already scanning'}
    threading.Thread(target=do_scan,args=(session['username'],),kwargs={'force_retry':True},daemon=True).start()
    return {'ok':True}

def main():
    if os.name!='nt':
        raise RuntimeError('This release requires Windows for DPAPI and desktop notifications.')
    print('My AI Secretary - Mail Edition')
    if WEB_BIND_HOST not in ('127.0.0.1','localhost') and not USE_HTTPS and not ALLOW_INSECURE_HTTP:
        raise RuntimeError('Remote HTTP is blocked. Configure WEB_TLS_CERT/WEB_TLS_KEY or explicitly set ALLOW_INSECURE_WEB_HTTP=true for isolated testing.')
    if bool(WEB_TLS_CERT) != bool(WEB_TLS_KEY):
        raise RuntimeError('WEB_TLS_CERT and WEB_TLS_KEY must both be set.')
    print(f'Open: {BASE_URL}')
    if not USE_HTTPS:print('WARNING: HTTP login credentials and mail content are not encrypted over the network!')
    if os.getenv('ALLOW_PLAINTEXT_IMAP','false').lower()!='true':
        print('WARNING: Plain IMAP is disabled. See .env and README.')
    threading.Timer(1.8,lambda:webbrowser.open(BASE_URL)).start()
    uvicorn.run(app,host=WEB_BIND_HOST,port=PORT,log_level='warning',ssl_certfile=WEB_TLS_CERT or None,ssl_keyfile=WEB_TLS_KEY or None)

if __name__=='__main__':main()
