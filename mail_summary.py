"""Local mail summaries. No external API, no outbound network calls.

Extractive fallback is always available; optional localhost Ollama refines the
selected mail's summary. No AI reply draft is created.
"""
from __future__ import annotations
import hashlib
from functools import lru_cache
import json
import logging
import os
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

LOG = logging.getLogger('mailagent.summary')
KST = ZoneInfo('Asia/Seoul')

class _HTMLToText(HTMLParser):
    BLOCK = {'p','div','section','article','header','footer','blockquote','tr','ul','ol','table','h1','h2','h3','h4','h5','h6'}
    SKIP = {'style','script','template','iframe','svg','object'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out=[]; self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in self.SKIP: self.skip += 1
        if not self.skip:
            if tag in self.BLOCK: self.out.append('\n\n')
            elif tag=='br': self.out.append('\n')
            elif tag=='li': self.out.append('\n• ')
            elif tag in {'td','th'}: self.out.append('  ')
    def handle_endtag(self,tag):
        if tag in self.SKIP: self.skip=max(0,self.skip-1); return
        if not self.skip and tag in self.BLOCK: self.out.append('\n\n')
    def handle_data(self,data):
        if not self.skip: self.out.append(data)

def clean_body(value):
    text=str(value or '').replace('\r\n','\n').replace('\r','\n')
    if re.search(r'</?(?:p|div|span|br|table|tr|td|html|body|font|a|li|strong|b|ul|ol|h[1-6])(?:\s|>|/)',text,re.I):
        parser=_HTMLToText()
        try: parser.feed(text); text=''.join(parser.out)
        except Exception: pass
    text=unescape(text).replace('\xa0',' ')
    text=re.sub(r'[\t ]+\n','\n',text)
    text=re.sub(r'\n[\t ]+','\n',text)
    text=re.sub(r'(?m)^[ \t]+$', '', text)
    # Keep paragraphs, but collapse exaggerated empty lines.
    text=re.sub(r'\n{3,}','\n\n',text)
    # A wrapped line is joined only when it looks like a continuation, not a
    # heading, list, quoted mail or a sentence boundary.
    lines=text.split('\n'); out=[]
    list_start=re.compile(r'^(?:[-*•]|\d+[.)]|[가-힣][.)]|>|\|)\s*')
    for line in lines:
        line=line.strip()
        if (line and out and out[-1] and not list_start.match(line)
            and not list_start.match(out[-1])
            and not re.search(r'[.!?。！？:：;；]$|[\])}]$',out[-1])
            and len(out[-1])>=35 and len(line)>=12
            and not re.match(r'^(?:안녕하세요|감사합니다|수신|발신|제목|From:|To:|Date:)',line,re.I)):
            out[-1]+=' '+line
        else:
            out.append(line)
    return '\n'.join(out).strip()

def format_date(value):
    raw=str(value or '').strip()
    if not raw:return '—'
    try:
        dt=parsedate_to_datetime(raw)
    except (ValueError,TypeError,IndexError):
        try: dt=datetime.fromisoformat(raw.replace('Z','+00:00'))
        except ValueError:
            m=re.match(r'^(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::(\d\d))?',raw)
            return f'{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}:{m[6] or "00"}' if m else raw
    if dt.tzinfo is not None: dt=dt.astimezone(KST)
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def extract_summary(subject, body, max_chars=230):
    """Fast grounded extractive summary; does not invent absent facts."""
    text=clean_body(body)
    lines=[]
    for part in re.split(r'\n+|(?<=[.!?。！？])\s+',text):
        part=part.strip(' \t•-')
        if not part or re.match(r'^(?:보낸 사람|받는 사람|From:|To:|Sent:|Date:|수신:|발신:)',part,re.I):continue
        if re.match(r'^(?:안녕하세요|감사합니다|좋은 하루|Best regards|Regards|감사드)',part,re.I) and len(part)<55:continue
        if re.search(r'(?:회신 기한 안에 회신하지 못한 점|답변이 늦어 죄송|회신이 늦어 죄송|양해 부탁드립니다)',part) and len(part)<120:continue
        if re.match(r'^(?:https?://|www\.)',part,re.I):continue
        if len(part)<9:continue
        lines.append(part)
    if not lines:
        return clean_body(subject or '')[:max_chars] or '본문이 없어 요약할 내용이 없습니다.'
    keywords=('요청','확인','일정','변경','완료','진행','검토','조치','문의','필요','기한','오류','회의','회신','협의','공유','안내','수정','검증')
    ranked=sorted(enumerate(lines[:35]),key=lambda x:(-sum(x[1].count(w) for w in keywords),x[0]))
    selected=sorted([i for i,_ in ranked[:3]])
    summary=' / '.join(lines[i] for i in selected)
    return summary[:max_chars].rstrip(' /') + ('…' if len(summary)>max_chars else '')

SUMMARY_FIELDS = (
    ('main', '주요 내용'),
    ('request', '요청사항'),
    ('schedule', '일정·기한'),
    ('action', '후속 조치'),
)
MISSING = '메일에 명시되지 않음'


def fallback_structured_summary(subject, body):
    """Conservative deterministic fallback. Never assign the same generic sentence to all four fields."""
    text = clean_body(body)
    # Limit analysis of long reply chains; keep current message ahead of quoted history.
    text = re.split(r'(?im)^\s*(?:-----\s*Original Message\s*-----|보낸 사람\s*:|From\s*:|-----\s*전달된 메시지)', text, maxsplit=1)[0]
    chunks = [line.strip(' •-\t') for line in re.split(r'\n+|(?<=[.!?。！？])\s+', text) if line.strip()]
    fragments = [line for line in chunks if len(line) >= 7 and not re.match(
        r'^(?:안녕하세요|감사합니다|좋은 하루|Best regards|Regards|From:|To:|Date:)', line, re.I)]
    main = extract_summary(subject, text, max_chars=210)
    if not fragments and subject:
        main = clean_body(subject)[:210]
    # Distinguish an explicit request from greetings, apologetic formulas and generic acknowledgements.
    request_tokens = ('요청드립니다', '요청합니다', '부탁드립니다', '부탁합니다', '확인해 주세요', '확인해주시',
                      '알려주세요', '알려주시', '회신해', '회신 부탁', '제출해', '공유해', '보내주시',
                      '검토해', '참석해', '답변 부탁', 'please', '문의드립니다')
    def actual_request(line):
        lower = line.lower()
        return any(token in lower for token in request_tokens) and not re.search(
            r'^(?:회신 기한 안에 회신하지 못한 점|늦게 회신드려 죄송|회신이 늦어 죄송)', line)
    request = next((x[:210] for x in fragments if actual_request(x)), MISSING)
    # Schedule must have a concrete date/time/deadline, not merely the word '일정'.
    date_pattern = re.compile(r'(?:\d{4}[-./년]\s*\d{1,2}(?:[-./월]\s*\d{1,2}일?)?|'
                              r'\d{1,2}월\s*\d{1,2}일|\d{1,2}[/.-]\d{1,2}|'
                              r'\d{1,2}:\d{2}|\d{1,2}시|\d{1,2}일까지|금일|내일|익일|차주|deadline)', re.I)
    schedule = next((x[:210] for x in fragments if date_pattern.search(x)), MISSING)
    # An action is not inferred from the existence of a request alone.
    action_tokens = ('조치하겠습니다', '진행하겠습니다', '전달하겠습니다', '공유하겠습니다',
                     '검토하겠습니다', '처리하겠습니다', '예정입니다', '진행 예정',
                     '후속 조치', '확인 후', '검토 후', '처리 후')
    action = next((x[:210] for x in fragments if any(t in x.lower() for t in action_tokens)
                   and x != request and x != schedule), MISSING)
    return {'main': main, 'request': request, 'schedule': schedule, 'action': action}


def validate_structured_summary(raw):
    """Accept strictly one JSON object with all four text fields."""
    if isinstance(raw, str):
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.S | re.I).strip()
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', raw, re.I)
        if match:
            raw = match.group(1).strip()
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError('AI 응답이 유효한 JSON이 아닙니다') from exc
    if not isinstance(raw, dict):
        raise ValueError('AI 응답이 JSON 객체가 아닙니다')
    out = {}
    for key, _ in SUMMARY_FIELDS:
        value = raw.get(key)
        if not isinstance(value, str):
            raise ValueError(f'요약 필드 누락: {key}')
        value = clean_body(value).strip()
        if not value or len(value) > 800:
            raise ValueError(f'요약 필드 길이 오류: {key}')
        out[key] = value
    return out


def structured_summary_with_ollama(subject, body):
    """Return summary + truthful status/model for display; no generated reply."""
    model = (os.getenv('MAIL_SUMMARY_MODEL') or os.getenv('OLLAMA_MODEL') or 'qwen3:1.7b').strip()
    fallback = fallback_structured_summary(subject, body)
    base = {'summary': fallback, 'model': model, 'status': 'fallback',
            'status_label': '추출 요약 · AI 분석 미완료', 'error_category': None}
    if not clean_body(body):
        return {**base, 'status': 'no_body', 'status_label': '본문 없음 · 추출 요약'}
    if os.getenv('MAIL_SUMMARY_AI', 'true').lower() != 'true':
        return {**base, 'status': 'disabled', 'status_label': 'AI 분석 비활성화 · 추출 요약'}
    url = os.getenv('OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
    if not re.fullmatch(r'https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?', url, re.I):
        LOG.warning('Summary endpoint not localhost; extractive fallback')
        return {**base, 'status_label': 'AI 연결 설정 오류 · 추출 요약'}
    source = clean_body(body)
    prompt = (
        '당신은 한국어 업무 메일의 사실 기반 정보 추출기입니다. 회신 초안을 작성하지 마세요.\n'
        '반드시 JSON 객체 하나만 반환하세요. Markdown, 추가 설명, <think>를 출력하지 마세요.\n'
        '정확히 네 개의 문자열 키만 포함하세요: main, request, schedule, action.\n'
        'main=메일의 주요 내용, request=발신자가 요구하거나 문의한 내용, '
        'schedule=원문에 명시된 일정이나 기한, action=원문에 명시된 후속 조치.\n'
        '각 항목은 간결한 한국어 1~2문장으로 작성하고 중요한 날짜·수치·담당자를 원문 그대로 사용하세요.\n'
        '해당 정보가 원문에 없으면 정확히 "메일에 명시되지 않음"이라고 작성하세요.\n'
        '답변을 생성하지 말고 사실만 분류하세요. 요청사항에는 실제 요청만, 일정에는 날짜 또는 기한이 있는 정보만, 후속 조치에는 명시된 계획·행동만 넣으세요. '
        '사과 문구나 동일 문장을 여러 필드에 복사하지 마세요. '
        '인용된 이전 회신·서명·광고가 아니라 현재 메일의 핵심을 우선하세요. '
        '원문에 없는 담당자, 약속, 완료 상태, 일정 또는 계획은 절대 추측하지 마세요.\n'
        f'제목: {str(subject or "")[:300]}\n본문:\n{source[:6500]}'
    )
    try:
        import requests
        result = requests.post(url + '/api/chat', json={
            'model': model, 'stream': False, 'format': 'json',
            'messages': [
                {'role': 'system', 'content': '입력 메일의 사실만 추출하고 JSON 객체로 답변합니다.'},
                {'role': 'user', 'content': prompt},
            ],
            'think': False,
            'options': {'temperature': 0.0, 'num_predict': 650, 'num_ctx': 8192},
        }, timeout=(5, int(os.getenv('MAIL_SUMMARY_TIMEOUT','180'))))
        result.raise_for_status()
        content = (result.json().get('message') or {}).get('content', '')
        summary = validate_structured_summary(content)
        LOG.info('Mail summary generated successfully with model %s', model)
        return {'summary': summary, 'model': model, 'status': 'success',
                'status_label': 'AI 분석 성공'}
    except Exception as exc:
        category = type(exc).__name__
        detail = str(exc)
        LOG.warning('Mail summary failed (model=%s, reason=%s): %s', model, category, detail[:350])
        return {**base, 'error_category': category,
                'status_label': 'AI 분석 실패 · 추출 요약'}


# Legacy compatibility if older callers exist.
def summarise_with_ollama(subject, body, fallback):
    result = structured_summary_with_ollama(subject, body)
    summary = ' / '.join(result['summary'][key] for key, _ in SUMMARY_FIELDS)
    return summary, ('로컬 AI 요약' if result['status'] == 'success' else '추출 요약')


@lru_cache(maxsize=256)
def cached_structured_summary(subject, body, model_name, ai_enabled, server_url):
    """Cache successful summaries only through the caller wrapper (failures not sticky)."""
    return structured_summary_with_ollama(subject, body)


def summary_for_display(subject, body):
    """Avoid expensive repeated requests; do not cache failures or partial outputs."""
    model = (os.getenv('MAIL_SUMMARY_MODEL') or os.getenv('OLLAMA_MODEL') or 'qwen3:1.7b').strip()
    enabled = os.getenv('MAIL_SUMMARY_AI', 'true').lower()
    url = os.getenv('OLLAMA_URL', 'http://127.0.0.1:11434')
    result = cached_structured_summary(subject or '', body or '', model, enabled, url)
    if result['status'] != 'success':
        cached_structured_summary.cache_clear()
    return result
