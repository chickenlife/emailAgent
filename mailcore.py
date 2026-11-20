import email
import base64
import quopri
import imaplib
import logging
import re
import binascii
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from html import unescape
from pathlib import Path
import os

import storage

log=logging.getLogger('mailagent')
HOST=os.getenv('IMAP_HOST','email.atec.kr')
PORT=int(os.getenv('IMAP_PORT','143'))
INBOX=os.getenv('IMAP_FOLDER','INBOX')
SENT=os.getenv('SENT_FOLDER','AUTO')
HISTORY_LIMIT=int(os.getenv('HISTORY_LIMIT_PER_FOLDER','0'))
RAG_ALL_FOLDERS=os.getenv('RAG_ALL_FOLDERS','true').strip().lower()=='true'
MAX_BODY_PART_BYTES=int(os.getenv('MAX_BODY_PART_BYTES','1048576'))
MAX_TOTAL_BODY_BYTES=int(os.getenv('MAX_TOTAL_BODY_BYTES','2097152'))
MAX_BODY_PARTS=int(os.getenv('MAX_BODY_PARTS','12'))
RETRY_BATCH_SIZE=max(1,min(200,int(os.getenv('RETRY_BATCH_SIZE','20'))))
# First login: index newest mail first and progressively backfill old mail.
FAST_INITIAL_INDEX=os.getenv('FAST_INITIAL_INDEX','true').strip().lower()=='true'
INITIAL_INDEX_BATCH=max(1,min(1000,int(os.getenv('INITIAL_INDEX_BATCH','100'))))
HISTORY_BACKFILL_BATCH=max(1,min(1000,int(os.getenv('HISTORY_BACKFILL_BATCH','150'))))


def decode(value):
    if not value:return ''
    try:return str(make_header(decode_header(value)))
    except Exception:return str(value)


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__();self.parts=[];self.ignore=0
    def handle_starttag(self,tag,attrs):
        if tag in ('style','script'):self.ignore+=1
        if tag in ('p','div','li','br','tr'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('style','script'):self.ignore=max(0,self.ignore-1)
    def handle_data(self,data):
        if not self.ignore:self.parts.append(data)


def normalize_body(value):
    """Decode HTML entities; strip visible HTML markup safely as plain text."""
    value=value or ''
    # Text/plain messages can occasionally contain HTML markup.
    if re.search(r'<(?:p|div|span|br|table|tr|td|html|body|font|a)\b',value,re.I):
        parser=HTMLText()
        try:
            parser.feed(value)
            value=''.join(parser.parts)
        except Exception:
            value=re.sub(r'<[^>]{1,300}>','',value)
    value=unescape(unescape(value)).replace('\xa0',' ')
    return re.sub(r'\n{4,}','\n\n',value).strip()


def extract_message(raw, unread=False):
    msg=email.message_from_bytes(raw)
    parts=[];html=[]
    for p in msg.walk():
        if p.get_content_disposition()=='attachment':continue
        kind=p.get_content_type()
        if kind not in ('text/plain','text/html'):continue
        payload=p.get_payload(decode=True)
        if payload is None:continue
        if len(payload)>MAX_BODY_PART_BYTES:continue
        try: content=payload.decode(p.get_content_charset() or 'utf-8',errors='replace')
        except LookupError:content=payload.decode('utf-8',errors='replace')
        if kind=='text/plain':parts.append(content)
        else:html.append(content)
    if parts: body='\n'.join(parts)
    else:
        parser=HTMLText()
        for part in html:parser.feed(part)
        body=''.join(parser.parts)
    body=normalize_body(body)[:12000]
    def addr(header):
        return ', '.join(f'{decode(name)} <{address}>' if name else address
                         for name,address in getaddresses([msg.get(header,'')]))
    return {'msgid':msg.get('Message-ID',''),'in_reply_to':msg.get('In-Reply-To',''),'references':msg.get('References',''),'subject':decode(msg.get('Subject')),
            'sender':addr('From'),'recipients':addr('To'),'cc':addr('Cc'),
            'date_text':msg.get('Date',''),'body':body,'is_unread':unread}



def target_email(username):
    """Mailbox identity for filtering inbound mail; never use substring matching."""
    configured=os.getenv('TARGET_EMAIL','').strip()
    value=configured or username.strip()
    parsed=parseaddr(value)[1].strip().casefold()
    if not parsed or '@' not in parsed or not parsed.split('@',1)[0] or '.' not in parsed.split('@',1)[1]:
        raise RuntimeError(
            'TARGET_EMAIL must be set to the user email address in .env '
            '(or log in with the full email address).'
        )
    return parsed


def matching_recipient(msg, target):
    """Only exact To/Cc addresses count. Bcc cannot reliably be found in MIME."""
    headers=[msg.get('recipients') or '',msg.get('cc') or '']
    return any(address.strip().casefold()==target
               for _name,address in getaddresses(headers) if address)


def matching_sender(msg,target):
    return any(address.strip().casefold()==target
               for _name,address in getaddresses([msg.get('sender') or '']) if address)

def connect(username,password):
    # Explicit opt-in: this sends username/password and mail content without TLS.
    if os.getenv('ALLOW_PLAINTEXT_IMAP','false').lower()!='true':
        raise RuntimeError('Plaintext IMAP disabled. Set ALLOW_PLAINTEXT_IMAP=true only in approved test LAN.')
    client=imaplib.IMAP4(HOST,PORT,timeout=20)
    try:
        typ,_=client.login(username,password)
        if typ!='OK':raise RuntimeError('IMAP login rejected')
        return client
    except Exception:
        try:client.shutdown()
        except Exception:pass
        raise


def close(client):
    try:client.logout()
    except Exception:
        try:client.shutdown()
        except Exception:pass


def list_folders(client):
    typ,data=client.list()
    if typ!='OK':return []
    result=[]
    for raw in data or []:
        if not isinstance(raw,bytes):continue
        line=raw.decode('utf-8','replace')
        # IMAP LIST response: (\\Sent) "/" "Sent Items"
        m=re.match(r'^\((.*?)\)\s+(?:"[^"]*"|NIL)\s+(.+)$',line)
        if not m:continue
        flags=m.group(1)
        folder=m.group(2).strip().strip('"')
        result.append((folder,flags))
    return result


def sent_folder(client):
    folders=list_folders(client)
    if SENT!='AUTO':return SENT
    for f,flags in folders:
        if '\\sent' in flags.lower():return f
    for name in ('Sent','Sent Items','INBOX.Sent','INBOX/Sent','보낸편지함','Sent Messages'):
        for f,_ in folders:
            if f.lower()==name.lower():return f
    return None


# IMAP BODYSTRUCTURE parser: only the MIME envelope/structure is fetched first.
# Attachment byte streams are never requested from the IMAP server.
class BodyStructureError(ValueError):
    pass


def _tokens(data):
    """Tokenize BODYSTRUCTURE as an IMAP parenthesized list (RFC 3501)."""
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c in b' \r\n\t':
            i += 1
            continue
        if c == 40:  # (
            yield '('
            i += 1
        elif c == 41:  # )
            yield ')'
            i += 1
        elif c == 34:  # quoted string
            i += 1
            out = bytearray()
            while i < n:
                if data[i] == 92:
                    i += 1
                    if i >= n:
                        raise BodyStructureError('Invalid quoted escape')
                    out.append(data[i])
                    i += 1
                elif data[i] == 34:
                    i += 1
                    break
                else:
                    out.append(data[i])
                    i += 1
            else:
                raise BodyStructureError('Unclosed quoted string')
            yield out.decode('utf-8', 'replace')
        elif c == 123:  # RFC 3501 literal in response
            end = data.find(b'}\r\n', i)
            if end < 0:
                raise BodyStructureError('Unsupported BODYSTRUCTURE literal')
            length = int(data[i+1:end])
            start = end + 3
            if length > 65536 or start + length > n:
                raise BodyStructureError('Invalid BODYSTRUCTURE literal length')
            yield data[start:start+length].decode('utf-8', 'replace')
            i = start + length
        else:
            end = i
            while end < n and data[end] not in b' ()\r\n\t':
                end += 1
            item = data[i:end].decode('ascii', 'replace')
            if item.upper() == 'NIL':
                yield None
            elif item.isdigit():
                yield int(item)
            else:
                yield item
            i = end


def _parse_list(tokens):
    iterator = iter(tokens)

    def parse(depth=0):
        if depth > 32:
            raise BodyStructureError('MIME nesting too deep')
        result = []
        for token in iterator:
            if token == ')':
                return result
            if token == '(':
                result.append(parse(depth+1))
            else:
                result.append(token)
        raise BodyStructureError('Unclosed BODYSTRUCTURE list')

    for token in iterator:
        if token == '(':
            return parse()
    raise BodyStructureError('BODYSTRUCTURE list missing')


def _structure(client, uid):
    typ, data = client.uid('FETCH', str(uid), '(BODYSTRUCTURE)')
    if typ != 'OK':
        raise RuntimeError(f'BODYSTRUCTURE FETCH failed: {uid}')
    for item in data or []:
        if isinstance(item, tuple):
            item = item[0]
        if not isinstance(item, bytes):
            continue
        match = re.search(rb'\bBODYSTRUCTURE\s*\(', item, re.I)
        if match:
            return _parse_list(_tokens(item[match.end()-1:]))
    raise BodyStructureError('BODYSTRUCTURE absent; refusing full-message fallback')


def _parameters(value):
    if not isinstance(value, list):
        return {}
    return {str(value[i]).lower(): value[i+1] for i in range(0, len(value)-1, 2)
            if isinstance(value[i], str)}


def _mime_parts(node, prefix=''):
    if not isinstance(node, list) or not node:
        return []
    if isinstance(node[0], list):
        # multipart: a sequence of nested BODYSTRUCTURE parts followed by subtype
        children = []
        i = 0
        while i < len(node) and isinstance(node[i], list):
            children.extend(_mime_parts(node[i], f'{prefix}.{i+1}' if prefix else str(i+1)))
            i += 1
        return children
    if len(node) < 7:
        return []
    major, minor = str(node[0]).lower(), str(node[1]).lower()
    if major == 'message' and minor == 'rfc822':
        # Do not recursively index attached/forwarded .eml messages
        return []
    if major != 'text' or minor not in ('plain', 'html'):
        return []
    params = _parameters(node[2])
    # RFC 3501 text BODYSTRUCTURE disposition at index 9 (index 7 is lines, 8 MD5)
    disposition = node[9] if len(node) > 9 else None
    if isinstance(disposition, list) and disposition:
        if str(disposition[0]).lower() == 'attachment':
            return []
        disp_params = _parameters(disposition[1]) if len(disposition) > 1 else {}
    else:
        disp_params = {}
    if params.get('name') or disp_params.get('filename'):
        return []
    size = node[6]
    if not isinstance(size, int) or size < 0:
        return []
    return [{'section': prefix or '1', 'kind': minor, 'charset': params.get('charset') or 'utf-8',
             'encoding': str(node[5] or '7BIT').upper(), 'size': size}]


def _fetch_literal(client, uid, selector, partial=None):
    """Fetch only the explicitly named MIME text part, never an entire multipart mail.

    Servers differ in their partial-FETCH reply labels (e.g. BODY[1]<0>).
    The literal is taken from the matching FETCH tuple, not a naked response line.
    """
    range_spec = f'<0.{partial}>' if partial is not None else ''
    typ, data = client.uid('FETCH', str(uid), f'(BODY.PEEK[{selector}]{range_spec})')
    if typ != 'OK':
        raise RuntimeError(f'FETCH failed for {selector}: {uid}')
    normalized=re.sub(r'\s+', '', selector).upper().encode('ascii')
    for item in data or []:
        if not (isinstance(item, tuple) and len(item)>=2 and isinstance(item[1],bytes)):
            continue
        head=item[0] if isinstance(item[0],bytes) else b''
        # Avoid mistakenly consuming a literal for an unrelated section.
        found=re.search(rb'BODY(?:\.PEEK)?\[([^]]+)\]',head,re.I)
        if found and re.sub(rb'\s+',b'',found.group(1)).upper()!=normalized:
            continue
        return item[1]
    raise RuntimeError(f'Missing IMAP section {selector}: {uid}')


def _decode_part(raw, part):
    encoding=part['encoding']
    if encoding=='BASE64':
        # A partial FETCH can cut a base64 quantum in the middle. Strip whitespace,
        # discard that incomplete tail and pad complete final quanta safely.
        clean=re.sub(rb'\s+',b'',raw)
        clean=re.sub(rb'[^A-Za-z0-9+/=]',b'',clean)
        if b'=' in clean:
            clean=clean[:clean.find(b'=')].rstrip(b'=')
        clean=clean[:len(clean)//4*4] if len(clean)%4==1 else clean
        clean+=b'='*((-len(clean))%4)
        try:
            decoded=base64.b64decode(clean,validate=False)
        except (binascii.Error,ValueError) as exc:
            log.warning('Malformed base64 in text MIME part, using best-effort recovery: %s',exc)
            compact=re.sub(rb'[^A-Za-z0-9+/]',b'',raw)
            compact=compact[:len(compact)//4*4]
            decoded=base64.b64decode(compact,validate=False) if compact else b''
    elif encoding=='QUOTED-PRINTABLE':
        decoded=quopri.decodestring(raw)
    else:
        decoded=raw
    decoded=decoded[:MAX_TOTAL_BODY_BYTES]
    charset=str(part['charset'])
    try:return decoded.decode(charset,'replace')
    except (LookupError,ValueError):return decoded.decode('utf-8','replace')


def _fetch_text_part(client, uid, part, is_single_root=False):
    """Fetch only bounded text data, with safe alternatives for older IMAP servers.

    For a single-part root, BODY[TEXT] is the RFC-standard text body selector.
    Multipart BODY[TEXT] includes attachments, so it must NEVER be used there.
    """
    selector=part['section']
    allowed=min(MAX_BODY_PART_BYTES,MAX_TOTAL_BODY_BYTES)
    if allowed<=0:return b''
    size=part['size']
    sections=['TEXT',selector] if is_single_root and selector=='1' else [selector]
    requests=[]
    for section in dict.fromkeys(sections):
        requests.append((section,allowed))
        if size<=allowed:
            requests.append((section,None))
    errors=[]
    for section,partial in requests:
        try:
            raw=_fetch_literal(client,uid,section,partial=partial)
            if len(raw)>allowed:
                raise RuntimeError('IMAP text section exceeded configured size limit')
            return raw
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError('Text part unavailable ('+'; '.join(errors[:4])+')')

def fetch_mail(client, uid, unread=False):
    # Fetch selected header fields and MIME structure, never BODY.PEEK[].
    header_fields = 'HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES SUBJECT FROM TO CC DATE)'
    try:
        head = _fetch_literal(client, uid, header_fields)
    except RuntimeError as primary_error:
        # Certain older IMAP servers do not support HEADER.FIELDS reliably,
        # even though an ordinary header-only FETCH succeeds. This fallback
        # reads headers only, never the message body or attachments.
        log.info('Mail UID %s: selective header fetch failed; trying full header only (%s)',
                 uid, primary_error)
        try:
            head = _fetch_literal(client, uid, 'HEADER')
        except RuntimeError as fallback_error:
            raise RuntimeError(
                f'Header unavailable (selective: {primary_error}; full-header: {fallback_error})'
            ) from fallback_error
    # Cap unusually large/malformed headers to avoid unbounded parsing.
    if len(head) > 256 * 1024:
        raise RuntimeError(f'IMAP header exceeds 256 KiB: {uid}')
    message = email.message_from_bytes(head)
    body_structure = _structure(client, uid)
    parts = _mime_parts(body_structure)
    single_root=bool(body_structure and not isinstance(body_structure[0],list))
    plain = [p for p in parts if p['kind'] == 'plain']
    html = [p for p in parts if p['kind'] == 'html']
    # Prefer text/plain and avoid downloading both HTML and plain alternatives.
    selected = plain if plain else html
    texts = []
    total = 0
    for part in selected[:MAX_BODY_PARTS]:
        remaining = MAX_TOTAL_BODY_BYTES - total
        if remaining <= 0:
            break
        # Request a bounded IMAP partial section; attached binary sections never requested.
        allowed = min(MAX_BODY_PART_BYTES, remaining)
        if allowed <= 0:
            break
        # A partial FETCH can return less than the requested section for short parts.
        section = part['section']
        try:
            raw = _fetch_text_part(client,uid,part,is_single_root=single_root)
        except RuntimeError as exc:
            if 'Missing IMAP section' in str(exc) and 'Text part unavailable' in str(exc):
                # Repeatedly retrying a permanently absent MIME section never helps.
                # Keep header metadata searchable; do not claim to have body evidence.
                log.info('Mail UID %s: missing text section; indexing header only', uid)
                continue
            raise
        raw = raw[:allowed]
        total += len(raw)
        decoded = _decode_part(raw, part)
        if part['kind'] == 'html':
            parser = HTMLText()
            parser.feed(decoded)
            decoded = ''.join(parser.parts)
        texts.append(decoded)
    def addr(header):
        return ', '.join(f'{decode(name)} <{address}>' if name else address
                         for name, address in getaddresses([message.get(header, '')]))
    body = normalize_body('\n'.join(texts))[:12000]
    if not body:
        log.info('Mail UID %s: no readable text body; header-only index (parts=%s)',uid,len(parts))
    return {'msgid': message.get('Message-ID', ''), 'in_reply_to': message.get('In-Reply-To', ''), 'references': message.get('References', ''), 'subject': decode(message.get('Subject')),
            'sender': addr('From'), 'recipients': addr('To'), 'cc': addr('Cc'),
            'date_text': message.get('Date', ''), 'body': body, 'is_unread': unread}


def scan_folder(client,account,folder,role,baseline_only=False,target=None,progress=None,force_retry=False):
    typ,_=client.select('"'+folder.replace('"','\\"')+'"',readonly=True)
    if typ!='OK':raise RuntimeError(f'Cannot select mailbox: {folder}')
    resp=client.response('UIDVALIDITY')
    validity=(resp[1][0].decode() if resp[1] and resp[1][0] else 'unknown')
    typ,data=client.uid('SEARCH',None,'ALL')
    if typ!='OK':raise RuntimeError('UID SEARCH ALL failed')
    uids=[int(x) for x in (data[0] or b'').split()]
    present=set(uids)
    current=storage.state(account,folder)
    newest=max(uids,default=0)
    reset=current is None or current['uidvalidity']!=validity
    # New accounts become usable after a small recent-mail baseline.
    # Older UIDs are backfilled in bounded batches with a separate cursor.
    backfill = storage.historical_backfill_state(account,folder,validity)
    fast_start = FAST_INITIAL_INDEX and HISTORY_LIMIT == 0
    if reset:
        if fast_start:
            pending = uids[-INITIAL_INDEX_BATCH:]
            before = min(pending) if pending else 0
            storage.set_historical_backfill(account,folder,validity,before,
                                            completed=not bool(uids[:-len(pending)] if pending else uids))
            backfill = storage.historical_backfill_state(account,folder,validity)
            log.info('Fast initial index folder=%s recent=%d total=%d; history continues in batches',
                     folder,len(pending),len(uids))
        else:
            pending = uids[-HISTORY_LIMIT:] if HISTORY_LIMIT>0 else uids
    else:
        pending = [x for x in uids if x>current['highest_uid']]

    # Backfill never generates new-mail alerts or AI drafts.
    older = []
    if (not reset and backfill and not backfill['completed'] and fast_start):
        earlier = [x for x in uids if x < backfill['next_before_uid']]
        older = earlier[-HISTORY_BACKFILL_BATCH:]
    storage.stale_scan_failures(account,folder,validity)
    if not reset and HISTORY_LIMIT == 0 and (not backfill or backfill['completed']):
        newly_queued=storage.enqueue_historical_gaps(
            account,folder,validity,uids,current['highest_uid'],role)
        if newly_queued:
            log.info('Historical RAG gaps queued: folder=%s count=%s',folder,newly_queued)
    # Always advance newest cursor; failed UIDs are stored in a durable retry queue.
    # Repeated scans therefore do not re-process every UID after one bad mail.
    due=storage.failed_scan_items(account,folder,validity,RETRY_BATCH_SIZE,force_retry)
    # Avoid duplicates for UIDs that also appear in this scan's new UID list.
    queued=[r for r in due if r['uid'] not in pending]
    unread_ids=set()
    if role=='inbox':
        typ,unseen=client.uid('SEARCH',None,'UNSEEN')
        if typ=='OK':unread_ids={int(x) for x in (unseen[0] or b'').split()}
    new_ids=[];failed=0;indexed=0
    work=[(uid,False,not reset) for uid in pending]
    # Old history is indexed without triggering notifications.
    work.extend((uid,False,False) for uid in older if uid not in pending)
    work.extend((r['uid'],True,bool(r['notify_on_recovery'])) for r in queued
                if r['uid'] not in older)
    if progress:progress(folder=folder,role=role,processed=0,total=len(work),failed=0,
                         uid=None,retrying=False,retry_pending=storage.scan_failure_count(account,folder))
    for i,(uid,is_retry,eligible) in enumerate(work,1):
        if progress:progress(folder=folder,role=role,processed=i-1,total=len(work),failed=failed,
                             uid=uid,retrying=is_retry,retry_pending=storage.scan_failure_count(account,folder))
        try:
            if uid not in present:
                # Mail deleted or moved from server; do not retry forever.
                storage.clear_scan_failure(account,folder,validity,uid)
                continue
            msg=fetch_mail(client,uid,uid in unread_ids)
            mid=storage.upsert(account,folder,validity,uid,role,msg)
            storage.clear_scan_failure(account,folder,validity,uid)
            indexed+=1
            # Draft every newly delivered INBOX message, regardless of To/Cc.
            # Initial historical baseline remains RAG-only.
            if role=='inbox' and eligible:
                new_ids.append(mid)
        except Exception as exc:
            failed+=1
            storage.mark_scan_failure(account,folder,validity,uid,role,str(exc),notify=eligible)
            log.warning('Mail UID %s skipped (queued for retry): %s',uid,exc)
        finally:
            if progress:progress(folder=folder,role=role,processed=i,total=len(work),failed=failed,
                                 uid=uid,retrying=is_retry,retry_pending=storage.scan_failure_count(account,folder))
    storage.set_state(account,folder,validity,newest)
    if older:
        # Continue from the next older UID on the following scan.
        storage.set_historical_backfill(account,folder,validity,min(older),
                                        completed=not bool([x for x in uids if x<min(older)]))
    elif not reset and backfill and not backfill['completed'] and not older:
        storage.set_historical_backfill(account,folder,validity,
                                        backfill['next_before_uid'],completed=True)
    bp=storage.historical_backfill_state(account,folder,validity)
    history_remaining=(sum(1 for x in uids if x < bp['next_before_uid'])
                       if bp and not bp['completed'] else 0)
    return new_ids,{'folder':folder,'indexed':indexed,'total':len(uids),'initial':reset,
                    'history_backfill_pending':bool(bp and not bp['completed']),
                    'history_remaining':history_remaining,
                    'failed':failed,'retry_remaining':storage.scan_failure_count(account,folder),
                    'retried':len(queued)}


def scan_priority_inbox(username, password, max_items=30):
    """Fast path for every newly arrived INBOX mail.

    Does not advance the historic scan cursor; the normal scan remains the
    source of truth and will re-upsert the same messages without duplicate
    draft IDs. Never treats old messages as new on initial baseline.
    """
    client = connect(username, password)
    try:
        typ, _ = client.select('"' + INBOX.replace('"', '\\"') + '"', readonly=True)
        if typ != 'OK':
            raise RuntimeError('Cannot select priority inbox')
        response = client.response('UIDVALIDITY')
        validity = response[1][0].decode() if response[1] and response[1][0] else 'unknown'
        state = storage.state(username, INBOX)
        if state is None or str(state['uidvalidity']) != str(validity):
            log.info('Priority inbox: baseline not established; pending initial index')
            return []
        typ, result = client.uid('SEARCH', None, 'ALL')
        if typ != 'OK':
            raise RuntimeError('Priority inbox UID search failed')
        new_uids = [int(x) for x in (result[0] or b'').split()
                    if int(x) > int(state['highest_uid'])]
        if len(new_uids) > max_items:
            log.warning('Priority inbox backlog: %s UIDs; limiting current cycle to %s',
                        len(new_uids), max_items)
        ids = []
        for uid in new_uids[:max_items]:
            try:
                msg = fetch_mail(client, uid, False)
                mid = storage.upsert(username, INBOX, validity, uid, 'inbox', msg)
                ids.append(mid)
            except Exception as exc:
                log.warning('Priority inbox UID %s deferred to regular scan: %s', uid, exc)
        return ids
    finally:
        close(client)


def check_connection(username,password):
    imap=connect(username,password)
    try:return {'folders':[x[0] for x in list_folders(imap)],'sent_folder':sent_folder(imap)}
    finally:close(imap)


def scan(username,password,progress=None,force_retry=False):
    """Index accessible mail folders for RAG; every new INBOX mail can trigger a draft.

    Archive/custom folders are reference-only. No IMAP flags are modified.
    """
    imap=connect(username,password)
    try:
        sent=sent_folder(imap)
        warnings=[]
        folder_results={}
        ids=[]
        # Scan INBOX first on each full cycle; a separate priority scan also runs.
        to_scan=[(INBOX,'inbox')]
        if sent and sent.casefold()!=INBOX.casefold():
            to_scan.append((sent,'sent'))
        elif not sent:
            warnings.append('Sent folder not auto-detected. Set SENT_FOLDER in .env.')
        if RAG_ALL_FOLDERS:
            skipped_flags=('\\noselect','\\trash','\\junk','\\drafts')
            skipped_names=('trash','deleted items','junk','spam','drafts','휴지통','스팸함','임시보관함')
            seen={name.casefold() for name,_ in to_scan}
            for name,flags in list_folders(imap):
                lowered=flags.lower()
                if any(flag in lowered for flag in skipped_flags):continue
                if name.strip('/').casefold() in skipped_names:continue
                if name.casefold() in seen:continue
                to_scan.append((name,'archive'))
                seen.add(name.casefold())
        for folder,role in to_scan:
            try:
                new,info=scan_folder(imap,username,folder,role,
                         progress=progress,force_retry=force_retry)
                folder_results[folder]=info
                if role=='inbox':ids.extend(new)
            except Exception as exc:
                warnings.append(f'{folder}: {exc}')
                log.warning('RAG folder scan failed %r: %s',folder,exc)
        return ids,{'inbox':folder_results.get(INBOX),
                    'sent':folder_results.get(sent) if sent else None,
                    'folders':folder_results,'warnings':warnings}
    finally:
        close(imap)
