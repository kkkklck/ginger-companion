"""Local persistence; optional PyMuPDF for original-image provenance. Never persists API keys."""
import json
import copy
import hashlib
import os
import re
import sqlite3
import threading
import unicodedata
import uuid
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager, closing
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('GINGER_DATA_DIR', ROOT / 'ginger_companion_data'))
LIBRARY_LOCK = threading.RLock()
LIBRARY_CACHE = None
LIBRARY_SIGNATURE = None
AI_SCIENCE_POLICY = 'ai-source-judgment-1'
SOURCE_BINDING_POLICY = 'original-text-or-page-image-1'
SEMANTIC_REVIEW_POLICIES = {
    'qualifiers-causality-1': {'quantifier', 'causality', 'uncertainty'},
    'qualifiers-causality-units-2': {'quantifier', 'causality', 'uncertainty', 'units', 'terminology'},
    'qualifiers-causality-units-source-3': {'quantifier', 'causality', 'uncertainty', 'units', 'terminology', 'source_fidelity', 'data_preservation'},
}
IMAGE_SOURCE_HASH_CACHE = {}


def source_anchor_is_structured(anchor, allow_images=False):
    """Check provenance shape, without treating AI transcription as PDF text."""
    if (not isinstance(anchor, dict) or type(anchor.get('page')) is not int
            or anchor['page'] < 1 or not isinstance(anchor.get('quote'), str)):
        return False
    if anchor.get('kind') == 'page_image':
        return (allow_images and anchor.get('block_id') == f"p{anchor['page']}-image"
                and bool(re.fullmatch(r'[a-f0-9]{64}', str(anchor.get('image_sha256', '')))))
    return len(anchor['quote'].strip()) >= 12


def image_sources_match_pdf(card, pdf_path, verified_pdf_hash):
    """Validate stored page fingerprints against the same original PDF at import."""
    anchors = list(card.get('evidence_anchors', [])) + list(card.get('source_image_context', []))
    gate = card.get('independent_gate', {})
    anchors.extend(gate.get('wording_check', {}).get('source_anchors', []))
    anchors.extend(a for c in gate.get('checks', []) for a in c.get('source_anchors', []))
    images = [a for a in anchors if a.get('kind') == 'page_image']
    if not images:
        return True
    try:
        import pymupdf
        missing = {a['page'] for a in images if (verified_pdf_hash, a['page']) not in IMAGE_SOURCE_HASH_CACHE}
        if missing:
            with pymupdf.open(str(pdf_path)) as document:
                for page in sorted(missing):
                    if not 1 <= page <= len(document):
                        return False
                    pixels = document[page - 1].get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False)
                    IMAGE_SOURCE_HASH_CACHE[verified_pdf_hash, page] = hashlib.sha256(pixels.samples).hexdigest()
        return all(IMAGE_SOURCE_HASH_CACHE.get((verified_pdf_hash, a['page'])) == a.get('image_sha256') for a in images)
    except (ImportError, OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def ai_scientific_review(card):
    return card.get('scientific_review_policy') == AI_SCIENCE_POLICY


def generic_boundary_only(value):
    def canonical(text):
        return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text))).strip('。，;；,.')
    return canonical(value) in {canonical(v) for v in (
        '仅限论文试验条件，未验证本田', '仅限论文试验条件', '仅限该研究条件，未验证本田',
        '研究条件内的结果，未验证其他田块', '仅限本研究条件', '仅限该研究条件',
        '仅限本研究试验条件，未验证本田', '仅限该试验条件，未验证本田')}


def evidence_card_digest(card):
    """Bind a second reader's decision to the exact final fact and its context."""
    fields = ('id', 'claim', 'evidence_type', 'scope', 'limitations', 'source_pointers', 'page_numbers')
    payload = {key: card.get(key) for key in fields}
    if card.get('protocol_policy'):
        payload['protocol_policy'] = card['protocol_policy']
        payload['source_fact_bindings'] = card.get('source_fact_bindings')
        if card.get('independent_source_review') is not None:
            payload['independent_source_review'] = card['independent_source_review']
    if ai_scientific_review(card):
        payload['scientific_review_policy'] = AI_SCIENCE_POLICY
    if 'requires_final_wording_check' in card:
        payload['requires_final_wording_check'] = card['requires_final_wording_check']
    if 'semantic_review_policy' in card:
        payload['semantic_review_policy'] = card['semantic_review_policy']
    if 'source_binding_policy' in card:
        payload['source_binding_policy'] = card['source_binding_policy']
        payload['image_source_bindings'] = [{k: a.get(k) for k in
            ('block_id', 'page', 'kind', 'image_sha256')} for a in card.get('evidence_anchors', [])
            if a.get('kind') == 'page_image']
        payload['source_image_context'] = card.get('source_image_context', [])
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode('utf-8')).hexdigest()


def evidence_claim_digest(card):
    text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(card.get('claim', ''))))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def evidence_is_withdrawn(card, source_hash):
    """Match explicit recorded withdrawals, without inferring scientific meaning."""
    path = DATA_DIR / 'evidence_withdrawals.json'
    if not path.is_file():
        return False
    rows = json.loads(path.read_text(encoding='utf-8')).get('records', [])
    fact_hash=protocol_digest({key:card.get(key) for key in ('claim','scope','limitations','evidence_type')})
    return any(row.get('source_pdf_sha256') == source_hash and (
        row.get('applies_to_identical_fact') is True and row.get('fact_sha256')==fact_hash
        or row.get('applies_to_identical_fact') is not True and row.get('source_card_id') == card.get('id')
            and row.get('claim_sha256') == evidence_claim_digest(card)) for row in rows)


def automatic_publication_hold(document):
    """Apply an institutional stop record, without judging paper content."""
    path=DATA_DIR/'factory_release_control.json'
    if not path.is_file():return None
    try:
        control=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(control,dict):raise ValueError('Invalid publication control')
        if control.get('status')!='paused' or document.get('protocol_pipeline')!=control.get('protocol_pipeline'):
            return None
        cutoff=datetime.fromisoformat(control['paused_at_utc'])
        created=datetime.fromisoformat(document.get('run_metadata',{}).get('created_at_utc',''))
        if cutoff.tzinfo is None or created.tzinfo is None:raise ValueError('Missing timezone')
        if created<cutoff:return None
        return control
    except (OSError,ValueError,KeyError,TypeError):
        return {'status':'paused','reason':'机构发布控制记录无法确认，结果保留待检查。'}


def protocol_independent_sources_valid(card, document):
    """Bind independent AI observations to their recorded image inputs."""
    if card.get('semantic_review_policy')!='qualifiers-causality-units-source-3':return True
    proof=card.get('independent_source_review',{})
    if proof.get('policy')=='direct-primary-pages-1':
        records=document.get('raw_extraction',{}).get('primary_source_reviews',[])
        ref=proof.get('review_binding',{})
        index=ref.get('index')
        if type(index) is not int or not 0<=index<len(records):return False
        record=records[index]
        if (record.get('policy')!='direct-primary-pages-1' or record.get('candidate_supplied') is not True
                or record.get('status')!='succeeded' or ref.get('sha256')!=protocol_digest(record)
                or record.get('source_pdf_sha256')!=document.get('source_pdf',{}).get('sha256')):return False
        inputs=record.get('candidate_inputs',[])
        expected=next((c for c in inputs if c.get('id')==card.get('id')),None)
        fields=('id','claim','scope','limitations','evidence_type','source_fact_ids','source_fact_bindings','page_numbers')
        if expected!={k:card.get(k) for k in fields}:return False
        row=next((r for r in record.get('output',{}).get('reviews',[]) if r.get('id')==card.get('id')),None)
        images=record.get('image_bindings',[])
        return (bool(images) and card.get('review_detail')==row and card.get('source_image_context')==images
            and proof.get('independent_fact_ids')==[])
    reads=document.get('raw_extraction',{}).get('independent_source_reads',[])
    refs=proof.get('read_bindings');ids=proof.get('independent_fact_ids')
    if (proof.get('policy')!='blind-page-reading-1' or not isinstance(refs,list) or not refs
            or not isinstance(ids,list) or not ids or len(set(ids))!=len(ids)):
        return False
    known=set();images=[];indices=[]
    for ref in refs:
        if not isinstance(ref,dict) or type(ref.get('index')) is not int or not 0<=ref['index']<len(reads):return False
        record=reads[ref['index']];indices.append(ref['index'])
        if (ref.get('sha256')!=protocol_digest(record) or record.get('status')!='succeeded'
                or record.get('source_pdf_sha256')!=document.get('source_pdf',{}).get('sha256')
                or record.get('policy')!='blind-page-reading-1'
                or record.get('candidate_supplied') is not False):return False
        output=record.get('output',{});target=record.get('target_page')
        states=output.get('page_states',[])
        if len(states)!=1 or states[0].get('page')!=target or states[0].get('status') not in {'read','excluded'}:return False
        known.update(f'BLIND-{target}-{f["id"]}' for o in output.get('source_objects',[]) for f in o.get('facts',[]))
        images.extend(record.get('image_bindings',[]))
    if len(set(indices))!=len(indices) or not set(ids)<=known:return False
    detail=card.get('review_detail',{})
    return (detail.get('independent_fact_ids')==ids
        and card.get('source_image_context')==list({b['page']:b for b in images}.values()))


def review_reason_conflict(check):
    """A positive flag cannot override an explicit negative final assessment."""
    if check.get('verdict') != 'supported':
        return False
    reason = str(check.get('reason', ''))
    return bool(re.search(
        r'(?:verdict|判定|结论)\s*(?:应为|为|是|:|：)\s*(?:unsupported|uncertain|不支持|无法支持)'
        r'|(?:这是|属于|存在|构成)(?:一个|具体的)?事实错误'
        r'|(?:claim|断言|该说法|此说法)[^。\n]{0,24}(?:没有|缺少|找不到)(?:直接)?(?:原文)?(?:依据|证据|支撑)',
        reason, re.I))


def assertion_review_issue(field_text, check, require=False):
    """Require lossless coverage, so a correct first half cannot hide an unchecked clause."""
    assertions = check.get('assertions')
    if assertions is None and not require:
        return None
    if not isinstance(assertions, list) or not 1 <= len(assertions) <= 32:
        return '缺少逐条断言检查'
    if any(not isinstance(a, dict) or not isinstance(a.get('text'), str)
           or not a['text'].strip() for a in assertions):
        return '断言检查格式无效'
    compact = lambda s: re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(s)))
    if compact(''.join(a['text'] for a in assertions)) != compact(field_text):
        return '断言未按顺序完整覆盖最终字段，须保留标点并逐段核查'
    for a in assertions:
        boundary = (a.get('verdict') == 'boundary_only' and check.get('field') == 'limitations'
                    and generic_boundary_only(a.get('text')) and not a.get('source_blocks'))
        if a.get('verdict') not in {'supported', 'unsupported', 'uncertain'} and not boundary:
            return '断言判定无效'
        if check.get('verdict') == 'supported' and a['verdict'] != 'supported' and not boundary:
            return '字段标记通过，但其中有未获支撑的断言'
        if not check.get('science_by_ai') and review_reason_conflict(a):
            return '断言标记通过，但复核依据指出事实错误'
        if a['verdict'] == 'supported' and (not a.get('source_blocks')
                or not set(a['source_blocks']) <= set(check.get('source_blocks', []))):
            return '断言缺少字段内的真实出处'
    return None


def independent_gate_is_valid(card):
    gate = card.get('independent_gate', {})
    if card.get('protocol_policy') and (
            card.get('protocol_policy') != 'source-facts-protocol-1'
            or gate.get('protocol_policy') != card['protocol_policy']
            or not card.get('source_fact_bindings')):
        return False
    if evidence_is_withdrawn(card, gate.get('source_pdf_sha256')):
        return False
    image_policy = card.get('source_binding_policy') == SOURCE_BINDING_POLICY and ai_scientific_review(card)
    if card.get('source_binding_policy') or gate.get('source_binding_policy'):
        if not image_policy or gate.get('source_binding_policy') != SOURCE_BINDING_POLICY:
            return False
    first_images = {(a.get('page'), a.get('image_sha256')) for a in card.get('evidence_anchors', [])
                    if source_anchor_is_structured(a, allow_images=image_policy) and a.get('kind') == 'page_image'}
    context = card.get('source_image_context', [])
    if not isinstance(context, list) or any(not source_anchor_is_structured(a, allow_images=image_policy)
            or a.get('kind') != 'page_image' for a in context):
        return False
    # A different citation within a page packet is not a different original source.
    # The context below is recorded from actual supplied PDF pages, never AI guesses.
    first_images.update((a['page'], a['image_sha256']) for a in context)

    def valid_anchor(a):
        if not image_policy and isinstance(a, dict) and a.get('kind') == 'page_image':
            # Older gates bound the page's real text as well as its image.
            # Preserve that text-backed protocol; it never permits empty scans.
            return (type(a.get('page')) is int and a['page'] > 0
                    and isinstance(a.get('quote'), str) and len(a['quote'].strip()) >= 12)
        return (source_anchor_is_structured(a, allow_images=image_policy)
                and (a.get('kind') != 'page_image' or (a['page'], a['image_sha256']) in first_images))
    if card.get('semantic_review_policy') or gate.get('semantic_review_policy'):
        required_semantics = SEMANTIC_REVIEW_POLICIES.get(card.get('semantic_review_policy'))
        if (not required_semantics
                or gate.get('semantic_review_policy') != card['semantic_review_policy']):
            return False
        semantic = gate.get('wording_check', {}).get('semantic_checks')
        if (not isinstance(semantic, list) or len(semantic) != len(required_semantics)
                or any(not isinstance(s, dict) for s in semantic)
                or {s.get('kind') for s in semantic} != required_semantics
                or any(s.get('verdict') not in {'consistent', 'not_applicable'}
                       or not isinstance(s.get('reason'), str) or not s['reason'].strip() for s in semantic)):
            return False
    if card.get('requires_final_wording_check') or gate.get('wording_policy') == 'whole-card-consistency-1':
        if gate.get('wording_policy') != 'whole-card-consistency-1':
            return False
        wording = gate.get('wording_check', {})
        if (wording.get('verdict') != 'consistent'
                or any(not isinstance(wording.get(k), str) or not wording[k].strip() for k in
                       ('candidate_readback', 'source_readback', 'reason'))
                or not wording.get('source_blocks')
                 or [a.get('block_id') for a in wording.get('source_anchors', [])] != wording['source_blocks']):
            return False
        if any(not valid_anchor(a) for a in wording.get('source_anchors', [])):
            return False
    checks = gate.get('checks', [])
    if (not isinstance(checks, list) or len(checks) != 3
            or any(not isinstance(check, dict) for check in checks)
            or {check.get('field') for check in checks} != {'claim', 'scope', 'limitations'}):
        return False
    for check in checks:
        if not ai_scientific_review(card) and review_reason_conflict(check):
            return False
        if check.get('verdict') == 'supported' and assertion_review_issue(
                card.get(check.get('field'), ''), check,
                require=gate.get('assertion_policy') == 'complete-clauses-1'):
            return False
        anchors = check.get('source_anchors', [])
        if check.get('verdict') == 'supported':
            if (not isinstance(anchors, list) or not anchors
                    or any(not valid_anchor(a) for a in anchors)
                    or [a.get('block_id') for a in anchors] != check.get('source_blocks')):
                return False
        elif check.get('verdict') == 'boundary_only' and check.get('field') == 'limitations':
            if not (card.get('protocol_policy') == 'source-facts-protocol-1'
                    and check.get('general_boundary_only') is True) and not generic_boundary_only(card.get('limitations')):
                return False
        else:
            return False
    return (gate.get('status') in ('verified', 'limited')
            and gate.get('source_pdf_sha256')
            and gate.get('reviewed_card_sha256') == evidence_card_digest(card))


class FactoryLedger:
    """Durable local work queue. Keys never enter this database."""
    def __init__(self, directory):
        self.path = Path(directory) / 'factory_jobs.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, input_path TEXT NOT NULL, input_sha256 TEXT NOT NULL,
                mode TEXT NOT NULL, policy TEXT NOT NULL, state TEXT NOT NULL,
                output_path TEXT, output_sha256 TEXT, updated_at TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '')''')

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            with conn:
                yield conn

    def enqueue(self, path, mode, policy):
        path = Path(path).resolve()
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        identity = hashlib.sha256(f'{mode}:{fingerprint}:{policy}'.encode()).hexdigest()
        with self.connect() as conn:
            conn.execute('''INSERT OR IGNORE INTO jobs
                (id,input_path,input_sha256,mode,policy,state,updated_at)
                VALUES(?,?,?,?,?,'pending',datetime('now'))''',
                (identity, str(path), fingerprint, mode, policy))
            conn.execute("UPDATE jobs SET input_path=? WHERE id=?", (str(path), identity))
            row = dict(conn.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone())
        return row

    def unfinished(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM jobs WHERE state IN ('pending','running','failed','partial') ORDER BY updated_at")]

    def supersede(self, identity):
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET state='superseded' WHERE id=? AND state!='running'", (identity,))

    def claim(self, identity):
        with self.connect() as conn:
            cur = conn.execute("UPDATE jobs SET state='running',updated_at=datetime('now') WHERE id=? AND state!='running'", (identity,))
            return cur.rowcount == 1

    def recover_interrupted(self):
        # Only called after the application's single-instance lock has been acquired.
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET state='pending',error='上次运行中断，可从保存的读数恢复' WHERE state='running'")

    def checkpoint(self, identity, output):
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET output_path=?,output_sha256=NULL,updated_at=datetime('now') WHERE id=?", (str(Path(output).resolve()), identity))

    def finish(self, identity, success, output, error='', state=None):
        path = Path(output)
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        with self.connect() as conn:
            conn.execute('''UPDATE jobs SET state=?,output_path=?,output_sha256=?,error=?,
                updated_at=datetime('now') WHERE id=?''',
                (state if state in {'succeeded', 'partial', 'failed'} else 'succeeded' if success else 'failed',
                 str(path.resolve()), fingerprint, error, identity))

    def discard_pending(self):
        with self.connect() as conn:
            conn.execute("DELETE FROM jobs WHERE state IN ('pending','failed','partial')")


def protocol_digest(value):
    """Content identity only; the hash must never decide a scientific answer."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def protocol_safe_request(payload):
    """Audit input without credentials or base64 image blobs."""
    def safe(value):
        if isinstance(value, dict):
            return {key: safe(child) for key, child in value.items()
                    if key not in {'_api_key', 'api_key', 'Authorization', 'authorization'}}
        if isinstance(value, list):
            return [safe(child) for child in value]
        if isinstance(value, str) and value.startswith('data:'):
            return {'attachment_sha256': hashlib.sha256(value.encode()).hexdigest()}
        return value
    return safe(payload)


def protocol_request_identity(payload, context):
    """Bind cache to actual inputs, replacing attachments by hashes and excluding secrets."""
    return protocol_digest({'request': protocol_safe_request(payload), 'context': protocol_safe_request(context)})


def protocol_cost_estimate(metadata, requested_model, prices):
    """Price recorded usage. Missing usage or an unknown returned model is not free."""
    model = metadata.get('model_returned') or requested_model
    usage = metadata.get('usage')
    profile = prices.get('models', {}).get(model)
    if not isinstance(usage, dict) or not isinstance(profile, dict):
        return {'status': 'unknown', 'estimated_cny': None, 'model': model,
                'reason': 'missing_usage_or_price', 'price_version': prices.get('version')}
    inp = usage.get('input_tokens', usage.get('prompt_tokens'))
    out = usage.get('output_tokens', usage.get('completion_tokens'))
    detail = usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
    cached = detail.get('cached_tokens', usage.get('cached_tokens', 0))
    if any(type(v) is not int or v < 0 for v in (inp, out, cached)) or cached > inp:
        return {'status': 'unknown', 'estimated_cny': None, 'model': model,
                'reason': 'invalid_or_incomplete_usage', 'price_version': prices.get('version')}
    try:
        rates = [Decimal(str(profile[k])) for k in
                 ('input_cny_per_million', 'output_cny_per_million', 'cached_input_cny_per_million')]
        if any(not rate.is_finite() or rate < 0 for rate in rates):
            raise ValueError('Invalid prices')
        cost = ((inp-cached)*rates[0] + out*rates[1] + cached*rates[2])/Decimal(1_000_000)
    except (KeyError, InvalidOperation, ValueError, TypeError):
        return {'status': 'unknown', 'estimated_cny': None, 'model': model,
                'reason': 'invalid_price_profile', 'price_version': prices.get('version')}
    return {'status': 'estimated', 'estimated_cny': str(cost), 'model': model,
            'input_tokens': inp, 'output_tokens': out, 'cached_tokens': cached,
            'price_version': prices.get('version'), 'billing_verified': False}


class ProtocolBudgetPaused(RuntimeError):
    pass


class ProtocolRequestPending(RuntimeError):
    pass


class ProtocolSourceBlocked(RuntimeError):
    pass


class ProtocolRequestLedger:
    """Durable per-request cache and cost journal, independent of the production library."""
    def __init__(self, directory, prices=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'protocol_requests.sqlite3'
        self.prices = copy.deepcopy(prices or {})
        with self.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS requests (
                identity TEXT PRIMARY KEY, paper_sha256 TEXT NOT NULL, stage TEXT NOT NULL,
                state TEXT NOT NULL, response_json TEXT, error TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            if 'input_json' not in {r[1] for r in conn.execute('PRAGMA table_info(requests)')}:
                conn.execute('ALTER TABLE requests ADD COLUMN input_json TEXT')
            conn.execute('''CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, identity TEXT NOT NULL, run_id TEXT NOT NULL,
                model TEXT NOT NULL, state TEXT NOT NULL, price_json TEXT NOT NULL,
                cost_json TEXT, metadata_json TEXT, error TEXT,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS budget_reservations (
                attempt_id TEXT PRIMARY KEY, upper_bound_cny TEXT NOT NULL,
                authorization TEXT NOT NULL, basis_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            with conn:
                yield conn

    def cached(self, identity):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM requests WHERE identity=?', (identity,)).fetchone()
            reserved=conn.execute('''SELECT 1 FROM attempts a JOIN budget_reservations b
                ON b.attempt_id=a.id WHERE a.identity=?''',(identity,)).fetchone()
        if not row:
            return None

        row = dict(row)
        if row['state'] == 'succeeded':
            try:
                response = json.loads(row['response_json'])
            except (TypeError, ValueError):
                response = None
            if not isinstance(response,dict) or not isinstance(response.get('ai_output'),dict):
                raise ProtocolRequestPending('历史请求标记成功但没有有效响应封包；费用未确认，保留记录，禁止自动重发。')
            return response
        if row['state'] in {'running', 'interrupted_unconfirmed'}:
            raise ProtocolRequestPending(row.get('error') or '上次请求中断，实际用量待确认，避免重复提交。')
        if row['state'] == 'blocked':
            raise ProtocolSourceBlocked(row.get('error') or '接口拒绝该来源，保留缺口，不更换切片绕过。')
        if row['state'] == 'invalid_format':
            raise RuntimeError(row.get('error') or '相同请求尚未确认结束，保留断点，避免重复费用。')
        if reserved:
            raise ProtocolRequestPending('历史请求仅按最高费用预留了预算，用量仍未知；禁止重发。')
        if row['state']=='failed':
            with self.connect() as conn:
                if self._unknown_attempt(conn,identity):
                    raise ProtocolRequestPending('上次请求失败且实际用量未知，保留断点，禁止自动重发。')
        return None

    @staticmethod
    def _unknown_attempt(conn,identity):
        for row in conn.execute('SELECT state,cost_json FROM attempts WHERE identity=?',(identity,)):
            cost=json.loads(row['cost_json']) if row['cost_json'] else {}
            if row['state']!='not_sent' and cost.get('estimated_cny') is None:return True
        return False

    def invalid_response(self, identity):
        with self.connect() as conn:
            row=conn.execute("SELECT response_json FROM requests WHERE identity=? AND state='invalid_format'",(identity,)).fetchone()
            if row and self._unknown_attempt(conn,identity):
                raise ProtocolRequestPending('上次格式缺口的实际用量未知，保留响应；不自动补格式或换图重发。')
        return json.loads(row['response_json']) if row and row['response_json'] else None

    def begin(self, identity, paper_hash, stage, model, run_id, budget_cny=None, request_bound_cny=None,input_record=None):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            prior = conn.execute('SELECT state FROM requests WHERE identity=?', (identity,)).fetchone()
            if conn.execute('''SELECT 1 FROM attempts a JOIN budget_reservations b
                    ON b.attempt_id=a.id WHERE a.identity=?''',(identity,)).fetchone():
                raise ProtocolRequestPending('预留预算不代表费用已核对，禁止重发历史未知请求。')
            if prior and prior['state'] in {'running', 'succeeded', 'blocked', 'interrupted_unconfirmed', 'invalid_format'}:
                raise RuntimeError('相同请求已存在，请先复用结果或核对中断状态。')
            if self._unknown_attempt(conn,identity):
                raise ProtocolRequestPending('相同请求已有费用未知的尝试，禁止自动重发。')
            attempts = conn.execute('SELECT COUNT(*) FROM attempts WHERE identity=?', (identity,)).fetchone()[0]
            if attempts >= 3:
                raise RuntimeError('相同请求已达到有限恢复次数，断点保留，不继续重复付费。')
            if budget_cny is not None:
                used, unknown = Decimal(0), False
                for row in conn.execute('''SELECT a.state,a.cost_json,b.upper_bound_cny
                        FROM attempts a LEFT JOIN budget_reservations b ON b.attempt_id=a.id'''):
                    cost = json.loads(row['cost_json']) if row['cost_json'] else {}
                    if cost.get('estimated_cny') is not None:
                        used += Decimal(cost['estimated_cny'])
                    elif row['state'] != 'not_sent':
                        if row['upper_bound_cny'] is not None:
                            used += Decimal(row['upper_bound_cny'])
                        else:
                            unknown = True
                if (unknown or request_bound_cny is None
                        or used + Decimal(str(request_bound_cny)) > Decimal(str(budget_cny))):
                    raise ProtocolBudgetPaused('实验预算已用完或有待确认费用，已保存断点；不是论文处理失败。')
            conn.execute('''INSERT INTO requests(identity,paper_sha256,stage,state)
                VALUES(?,?,?,'running') ON CONFLICT(identity) DO UPDATE SET
                state='running',error=NULL,updated_at=CURRENT_TIMESTAMP''', (identity, paper_hash, stage))
            if input_record is not None:
                conn.execute('UPDATE requests SET input_json=? WHERE identity=?',
                    (json.dumps(protocol_safe_request(input_record),ensure_ascii=False),identity))
            aid = uuid.uuid4().hex
            conn.execute('INSERT INTO attempts(id,identity,run_id,model,state,price_json) VALUES(?,?,?,?,?,?)',
                         (aid,identity,run_id,model,'running',json.dumps(self.prices,ensure_ascii=False)))
        return aid

    def reserve_unknown_cost(self, attempt_id, authorization):
        """Explicitly authorized budget allowance, never a fabricated billing receipt.

        A finished old attempt remains unknown and cannot be resubmitted. The bound
        uses its frozen price snapshot and the full model input ceiling, without
        discounts. Newly interrupted attempts still require a separate decision.
        """
        if not isinstance(authorization,str) or not authorization.strip():
            raise ValueError('必须记录用户明确授权；不能自动忽略未知费用。')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('''SELECT a.*,r.input_json FROM attempts a
                JOIN requests r ON r.identity=a.identity WHERE a.id=?''',(attempt_id,)).fetchone()
            if not row or row['state'] in {'running','not_sent'}:
                raise ValueError('只允许为已结束且费用未知的历史请求预留预算。')
            cost=json.loads(row['cost_json']) if row['cost_json'] else {}
            if cost.get('estimated_cny') is not None:
                raise ValueError('已有用量价格的请求无需预留未知费用。')
            prices=json.loads(row['price_json']);profile=prices.get('models',{}).get(row['model'],{})
            record=json.loads(row['input_json']) if row['input_json'] else {}
            completion=record.get('payload',{}).get('max_completion_tokens')
            ceiling=profile.get('max_input_tokens')
            if (prices.get('currency')!='CNY' or type(ceiling) is not int or ceiling<=0
                    or type(completion) is not int or completion<=0):
                raise ValueError('缺少冻结价格或上下文/输出上限，无法保守预留。')
            input_price=Decimal(str(profile.get('input_cny_per_million')))
            output_price=Decimal(str(profile.get('output_cny_per_million')))
            if any(not x.is_finite() or x<0 for x in (input_price,output_price)):
                raise ValueError('价格无效。')
            bound=(input_price*ceiling+output_price*completion)/Decimal(1_000_000)
            if bound<=0:raise ValueError('不能把未知费用预留为零。')
            basis={'model':row['model'],'price_version':prices.get('version'),
                'max_input_tokens':ceiling,'max_completion_tokens':completion,
                'input_cny_per_million':str(input_price),'output_cny_per_million':str(output_price),
                'billing_verified':False,'do_not_resubmit':True}
            conn.execute('''INSERT INTO budget_reservations(attempt_id,upper_bound_cny,authorization,basis_json)
                VALUES(?,?,?,?) ON CONFLICT(attempt_id) DO NOTHING''',
                (attempt_id,str(bound),authorization,json.dumps(basis,ensure_ascii=False)))
        return {'attempt_id':attempt_id,'upper_bound_cny':str(bound),'billing_verified':False}

    def finish(self, aid, response=None, error=None, blocked=False):
        if error is None and (not isinstance(response,dict) or 'ai_output' not in response):
            raise ValueError('没有有效模型响应封包，不能登记为成功请求。')
        metadata = copy.deepcopy((response or {}).get('api_metadata', {}))
        if error is not None:
            metadata = copy.deepcopy(getattr(error, 'api_metadata', {}) or metadata)
        state = 'blocked' if blocked else 'failed' if error is not None else 'succeeded'
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone()
            if not row or row['state'] != 'running':
                raise RuntimeError('请求记录已结束，不能重复计费。')
            cost = protocol_cost_estimate(metadata, row['model'], json.loads(row['price_json']))
            conn.execute('''UPDATE attempts SET state=?,metadata_json=?,cost_json=?,error=?,
                finished_at=CURRENT_TIMESTAMP WHERE id=?''',
                (state,json.dumps(metadata,ensure_ascii=False),json.dumps(cost,ensure_ascii=False),
                 str(error) if error else None,aid))
            conn.execute('''UPDATE requests SET state=?,response_json=?,error=?,
                updated_at=CURRENT_TIMESTAMP WHERE identity=?''',
                (state,json.dumps(response,ensure_ascii=False) if response is not None else None,
                 str(error) if error else None,row['identity']))

    def invalidate_format(self, identity, error):
        # The successful API attempt remains charged; only the parsed product is rejected.
        with self.connect() as conn:
            conn.execute("UPDATE requests SET state='invalid_format',error=? WHERE identity=?",
                         (str(error),identity))

    def recover_interrupted(self):
        # Call only after the single-instance application lock is acquired.
        with self.connect() as conn:
            conn.execute("UPDATE requests SET state='interrupted_unconfirmed',error='上次请求未确认结束，费用待确认' WHERE state='running'")
            conn.execute("UPDATE attempts SET state='interrupted_unconfirmed' WHERE state='running'")

    def summary(self, run_id=None):
        query, args = ('SELECT * FROM attempts', ())
        if run_id is not None:
            query += ' WHERE run_id=?'; args = (run_id,)
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(query,args)]
            reservations={r['attempt_id']:Decimal(r['upper_bound_cny'])
                          for r in conn.execute('SELECT * FROM budget_reservations')}
        known = Decimal(0); unknown = 0; tokens = 0
        reserved=Decimal(0);unreserved=0
        for row in rows:
            cost = json.loads(row['cost_json']) if row['cost_json'] else {}
            if cost.get('estimated_cny') is None:
                unknown += 1
                if row['id'] in reservations:reserved+=reservations[row['id']]
                else:unreserved+=1
            else:
                known += Decimal(cost['estimated_cny'])
            metadata = json.loads(row['metadata_json']) if row['metadata_json'] else {}
            usage = metadata.get('usage') or {}
            total = usage.get('total_tokens')
            if type(total) is int and total >= 0:
                tokens += total
        return {'api_calls':len(rows),'reported_tokens':tokens,'estimated_known_cny':str(known),
                'unknown_cost_requests':unknown,'currency_complete':unknown==0,
                'unknown_cost_reserved_upper_bound_cny':str(reserved),
                'unreserved_unknown_cost_requests':unreserved,
                'budget_accounted_upper_bound_cny':str(known+reserved) if not unreserved else None,
                'billing_verified':False,'cached_replay_not_charged_again':True}


def protocol_fact_bindings_valid(card, document):
    """Verify reference identity and content, without grading scientific meaning."""
    if not card.get('protocol_policy'):
        return True
    if card['protocol_policy'] != 'source-facts-protocol-1':
        return False
    bindings = card.get('source_fact_bindings')
    facts = document.get('raw_extraction', {}).get('source_facts', [])
    by_id = {f.get('id'):f for f in facts if isinstance(f,dict)}
    if (not isinstance(bindings,list) or not bindings or len(by_id)!=len(facts)
            or any(not isinstance(b,dict) for b in bindings)):
        return False
    ids=[b.get('id') for b in bindings]
    if len(set(ids))!=len(ids) or ids!=card.get('source_fact_ids'):
        return False
    expected=[f'/raw_extraction/source_facts/{next(i for i,f in enumerate(facts) if f["id"]==fid)}'
              for fid in ids if fid in by_id]
    return (expected==card.get('source_pointers') and len(expected)==len(ids)
            and all(b.get('id') in by_id and b.get('sha256') == protocol_digest(by_id[b['id']])
                    for b in bindings) and protocol_independent_sources_valid(card,document))


def inspect_protocol_requests(path, paper_sha256, namespace=''):
    """Read saved diagnostics without submitting requests or changing their states."""
    path=Path(path)
    if not path.is_file():raise FileNotFoundError('请求账本不在本地；不会新建空账本冒充零费用。')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)) as conn:
        conn.row_factory=sqlite3.Row
        prefix=namespace+':' if namespace else ''
        rows=[dict(r) for r in conn.execute('''SELECT a.*,r.stage,r.state AS request_state,
            r.error AS request_error,r.response_json,r.input_json FROM attempts a
            JOIN requests r ON a.identity=r.identity WHERE r.paper_sha256=?
            AND substr(r.stage,1,length(?))=? ORDER BY a.rowid''',(paper_sha256,prefix,prefix))]
    latest={r['identity']:r['id'] for r in rows}
    result=[];known=Decimal(0);unknown=0
    for row in rows:
        cost=json.loads(row['cost_json']) if row['cost_json'] else {}
        metadata=json.loads(row['metadata_json']) if row['metadata_json'] else {}
        record=json.loads(row['input_json']) if row['input_json'] else {}
        response=(json.loads(row['response_json']) if row['response_json'] else None) if latest[row['identity']]==row['id'] else None
        if cost.get('estimated_cny') is None:unknown+=1
        else:known+=Decimal(cost['estimated_cny'])
        data={}
        try:
            content=record.get('payload',{}).get('messages',[])[-1]['content']
            data=json.loads(content[-1]['text']) if isinstance(content,list) else {}
        except (IndexError,KeyError,TypeError,ValueError):pass
        result.append({'id':row['id'],'run_id':row['run_id'],'stage':row['stage'],
            'model':row['model'],'state':row['state'],'request_state':row['request_state'],
            'error':row['error'] or (row['request_error'] if latest[row['identity']]==row['id'] else ''),
            'started_at':row['started_at'],'finished_at':row['finished_at'],
            'request_identity':row['identity'],'cost':cost,'metadata':metadata,
            'target_pages':data.get('target_pages',[]) if isinstance(data,dict) else [],
            'response':protocol_safe_request(response),
            'response_is_latest_for_request':latest[row['identity']]==row['id']})
    return {'requests':result,'api_calls':len(rows),'estimated_known_cny':str(known),
            'unknown_cost_requests':unknown,'currency_complete':unknown==0,
            'billing_verified':False,'scope':'same_paper_and_namespace','network_calls':0}


def vibrational_assignment_issue(card, source_texts=None):
    """Check explicit spectral assignments against legible original table rows."""
    claim = unicodedata.normalize('NFKC', str(card.get('claim', '')))
    if not re.search(r'FTIR|FT-IR|波数|振动', claim, re.I):
        return None
    if source_texts is None:
        source_texts = [a.get('quote', '') for a in card.get('evidence_anchors', []) if isinstance(a, dict)]
    assignments = {}
    for text in source_texts:
        text = unicodedata.normalize('NFKC', str(text))
        for row in re.finditer(r'(?m)^\s*(\d{3,4}(?:\.\d+)?)\s*\n\s*(Stretching|Bending)\b', text, re.I):
            assignments.setdefault(Decimal(row[1]), set()).add(row[2].lower())
    for match in re.finditer(r'(伸缩|弯曲|stretching|bending)[^。；;()]{0,45}\(([^)]*)\)', claim, re.I):
        expected = 'bending' if match[1].lower() in ('弯曲', 'bending') else 'stretching'
        for value in re.findall(r'(?<!\d)\d{3,4}(?:\.\d+)?(?!\d)', match[2]):
            found = assignments.get(Decimal(value), set())
            if len(found) == 1 and expected not in found:
                correct = '伸缩' if 'stretching' in found else '弯曲'
                return f'波数{value}的振动类型与原表不符：原表为{correct}，不能和其他峰合并为同一类型'
    return None


def evidence_content_issue(card):
    """Shared admission guard for both the factory and the farmer's evidence library."""
    if ai_scientific_review(card):
        return None
    claim = str(card.get('claim', ''))
    assignment_issue = vibrational_assignment_issue(card)
    if assignment_issue:
        return assignment_issue
    for check in card.get('independent_gate', {}).get('checks', []):
        if isinstance(check, dict) and review_reason_conflict(check):
            return '复核结果与依据相矛盾：已指出事实错误却标记通过'
    scope_issue = attribution_scope_issue(card)
    if scope_issue:
        return scope_issue
    pair_issue = statistical_pair_issue(card)
    if pair_issue:
        return pair_issue
    if re.search(r'砂(?:质)?壤土\s*[（(]?\s*loamy\s+sand|loamy\s+sand\s*[）)]?\s*[（(]?\s*砂(?:质)?壤土', claim, re.I):
        return 'Loamy sand 的土壤质地术语翻译不一致：应核对壤质砂土/壤砂土；砂壤土对应 sandy loam'
    if re.search(r'(?i)修正\s*claim|claim\s*应|需修正为|应修正为|候选卡中|修订\s*claim', claim):
        return '结论混入复核指令或修订过程，需重新生成完整事实'
    if re.search(r'而非.*(?:品种|遗传)', str(card.get('limitations', ''))):
        return '未经排除实验不能声称差异不是由品种或遗传引起，删除该归因'
    # The label must not let numerical/statistical assertions bypass their checks.
    if (card.get('evidence_type') == 'author_interpretation'
            and re.search(r'显著|统计.*差异', claim)
            and re.search(r'\d+(?:\.\d+)?\s*(?:mg|µg|μg|个|条|cm)|统计字母', claim)
            and not card.get('statistical_checks')):
        return '作者解释中仍包含未经核对的数值统计结论'
    return None


def historical_standard_claim(card):
    if ai_scientific_review(card):
        return False  # The source reviewers decide applicability and reference status.
    text = str(card.get('claim', ''))
    # Standardised management, standard deviation and calibration curves are
    # research methods, not claims about a current regulatory limit.
    text = re.sub(r'标准化|标准差|标准误|标准曲线|标准溶液|标准品|标准培养基', '', text)
    return bool(re.search(r'标准|药典|FAO\s*/\s*WHO|\bSNI\b|\bISO\b', text, re.I))


def statistical_pair_issue(card):
    """Check every named pair in the prose, including pairs omitted from metadata."""
    claim = str(card.get('claim', ''))
    matrices = {}
    for check in card.get('statistical_checks', []):
        if not isinstance(check, dict):
            continue
        matrix = matrices.setdefault((check.get('metric'), check.get('block_id')), {})
        for group in check.get('groups', []):
            if isinstance(group, dict):
                matrix.setdefault(group.get('group'), set()).update(
                    re.findall('[a-z]', str(group.get('letters', '')).lower()))
    for sentence in re.split(r'[。；;]', claim):
        if not re.search(r'共享(?:统计)?字母|无显著差异|不显著不同', sentence):
            continue
        for left, right in re.findall(r'(?<![A-Za-z0-9])([A-Z][A-Z0-9_-]*)\s*(?:与|和|及)\s*([A-Z][A-Z0-9_-]*)(?![A-Za-z0-9])', sentence):
            matching = [groups for groups in matrices.values() if left in groups and right in groups]
            if matching and all(not groups[left] & groups[right] for groups in matching):
                return f'{left}与{right}没有共享统计字母，不能声称无显著差异'
    return None


def measurement_qualifier_issue(card, source_texts=None):
    """Keep a source's explicit time-specific estimation qualifier in the fact.

    This narrowly checks an original statement about composite growth values;
    it does not infer that every number on a page with regression is estimated.
    """
    if ai_scientific_review(card):
        return None
    claim = str(card.get('claim', ''))
    sources = '\n'.join(source_texts) if source_texts is not None else '\n'.join(
        str(a.get('quote', '')) for a in card.get('evidence_anchors', []) if isinstance(a, dict))
    # These are different measured quantities, not interchangeable translations.
    for value in re.findall(r'有机质(?:含量)?[^\d。]{0,12}(\d+(?:\.\d+)?)\s*%', claim):
        if (re.search(r'\bOC\s*' + re.escape(value) + r'\s*%', sources, re.I)
                and not re.search(r'(?:organic\s+matter|\bOM)\s*' + re.escape(value) + r'\s*%', sources, re.I)):
            return '原文该数值标为OC（有机碳），不能改成有机质含量'
    for label, formula, name in [('磷', 'P2O5', '五氧化二磷'), ('钾', 'K2O', '氧化钾')]:
        for value in re.findall(r'(?:有效|速效)' + label + r'[^\d。]{0,10}(\d+(?:\.\d+)?)\s*kg\s*/\s*ha', claim, re.I):
            if (re.search(re.escape(formula) + r'\s*[-:]?\s*' + re.escape(value) + r'\s*kg\s*/\s*ha', sources, re.I)
                    and not re.search(re.escape(formula) + '|' + name, claim, re.I)):
                return f'原文该数值以{formula}计，卡片遗漏了氧化物口径'
    if not re.search(r'复合.*?(?:生长|生物量|指数)|综合.*?生长.*?指数|composite', claim, re.I):
        return None
    for match in re.finditer(r'composite\s+(?:growth\s+)?(?:values?|scores?|indices|index)[^.;]{0,80}?'
                             r'(\d+)\s*(?:dpi|days?)[^.;]{0,100}?(?:estimated|predicted)', sources, re.I):
        day = match[1]
        if (re.search(r'(?<!\d)' + re.escape(day) + r'\s*(?:天|日|dpi|days?)', claim, re.I)
                and not re.search(r'估算|估计|预测|外推|回归|estimated|predicted', claim, re.I)):
            return f'原文明确说明{day}天复合生长值为估算/预测，事实遗漏了该限定'
    return None


def attribution_scope_issue(card):
    """Do not expand an original element list when attributing a transport mechanism."""
    claim = str(card.get('claim', ''))
    if not re.search(r'高移动性|高度移动|高流动性|易转运', claim):
        return None
    names = {'氮': 'N', '磷': 'P', '钾': 'K', '硫': 'S', '镁': 'Mg', '钙': 'Ca',
             '铁': 'Fe', '锌': 'Zn', '锰': 'Mn', '铜': 'Cu'}
    original = '\n'.join(a.get('quote', '') for a in card.get('evidence_anchors', []) if isinstance(a, dict))
    original = re.sub(r'(?<=\w)-\s*\n\s*(?=\w)', '', original)
    allowed = set()
    for m in re.finditer(r'elements?\s+(?:such as|including)\s+([^.;]{1,100}?)\s+(?:are|is)\s+highly\s+mobile', original, re.I):
        allowed.update(re.findall(r'\b(?:N|P|K|S|Mg|Ca|Fe|Zn|Mn|Cu)\b', m[1]))
    if not allowed:
        return None
    for sentence in re.split(r'[。；;]', claim):
        pos = re.search(r'高移动性|高度移动|高流动性|易转运', sentence)
        if not pos:
            continue
        asserted = {symbol for name, symbol in names.items() if name in sentence[:pos.start()]}
        extra = asserted - allowed
        if extra:
            return '机理解释扩大了原文适用对象：' + '、'.join(sorted(extra)) + '不在原文该机理的元素列表中'
    return None


def derivation_issue(card, source_texts=None):
    if ai_scientific_review(card):
        return None
    """Check concentration × mass across tables instead of trusting a printed intake."""
    checks = card.get('derivation_checks', [])
    claim = str(card.get('claim', ''))
    # Mentioning the purpose of a dry-mass conversion asserts no metal intake value.
    intake_value = (re.search(r'\d+(?:\.\d+)?\s*(?:mg|µg|μg|ug|毫克|微克)\s*(?:/\s*(?:day|d|天|日))', claim, re.I)
                    or re.search(r'(?:每日|日)(?:[^。；;]{0,15})摄入量\s*(?:为|是|约|[:：=])\s*\d', claim))
    if intake_value and not checks:
        return '每日摄入量缺少浓度、食用质量及单位的交叉核算'
    if not isinstance(checks, list):
        return '派生量核算格式无效'
    factors = {'ug/g': Decimal('0.001'), 'µg/g': Decimal('0.001'),
               'μg/g': Decimal('0.001'), 'mg/kg': Decimal('0.001'), 'mg/g': Decimal('1')}
    for check in checks:
        try:
            if check['operation'] != 'concentration_times_mass':
                return '不支持的派生量核算方式'
            concentrations = [Decimal(str(v)) for v in check['concentrations']]
            mass, reported = Decimal(str(check['mass'])), Decimal(str(check['reported']))
            if not concentrations or mass <= 0 or not all(v.is_finite() and v >= 0 for v in concentrations):
                return '派生量的原始浓度或质量无效'
            if check['mass_unit'] != 'g' or check['reported_unit'] != 'mg/day':
                return '派生量的质量或结果单位无法核算'
            factor = factors[check['concentration_unit']]
            expected = sum(concentrations) / len(concentrations) * mass * factor
            if not reported.is_finite() or abs(expected - reported) > max(abs(expected) * Decimal('0.02'), Decimal('0.000001')):
                return f"{check.get('quantity', '派生量')}与原浓度×质量不一致：原表写{reported}，核算为{expected} mg/day"
            if source_texts is not None:
                for key in ('input_block_id', 'result_block_id'):
                    block = re.fullmatch(r'p([1-9]\d*)', check[key])
                    if not block or int(block[1]) > len(source_texts):
                        return '派生量没有有效原文块'
                input_text = source_texts[int(check['input_block_id'][1:]) - 1]
                output_text = source_texts[int(check['result_block_id'][1:]) - 1]
                def has_value(value, text):
                    base = format(value.normalize(), 'f')
                    return bool(re.search(r'(?<![\d.])' + re.escape(base) + r'(?:0*)?(?![\d.])', text))
                if not all(has_value(v, input_text) for v in concentrations) or not has_value(reported, output_text):
                    return '派生量核算的浓度或结果未匹配原文'
        except (KeyError, TypeError, ValueError, InvalidOperation, ZeroDivisionError):
            return '派生量缺少原浓度、食用质量、单位或出处'
    return None


@contextmanager
def connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA_DIR / 'garden.sqlite3', timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, value TEXT NOT NULL)')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get(key, default=None):
    with connection() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else default


def put(key, value):
    with connection() as conn:
        conn.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (key, json.dumps(value, ensure_ascii=False)))


def put_weather(value):
    """A slow request must not replace the weather after switching fields."""
    with connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT value FROM settings WHERE key='field'").fetchone()
        field = json.loads(row['value']) if row else None
        if field and [round(field['lat'], 4), round(field['lon'], 4)] == value['location']:
            conn.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('weather', json.dumps(value, ensure_ascii=False)))


def add(kind, value):
    with connection() as conn:
        cursor = conn.execute('INSERT INTO entries (kind,value) VALUES (?,?)', (kind, json.dumps(value, ensure_ascii=False)))
        return cursor.lastrowid


def entries(kind):
    with connection() as conn:
        rows = conn.execute('SELECT id,value FROM entries WHERE kind=? ORDER BY id DESC', (kind,)).fetchall()
    return [dict(json.loads(row['value']), id=row['id']) for row in rows]


def remove(kind, entry_id):
    with connection() as conn:
        conn.execute('DELETE FROM entries WHERE kind=? AND id=?', (kind, entry_id))


def seed_demo(field, logs):
    """One transaction; cannot overwrite a user's field on repeated clicks."""
    with connection() as conn:
        if conn.execute("SELECT 1 FROM settings WHERE key='field'").fetchone():
            raise ValueError('已经有一片姜田了，演示不会覆盖您的记录。')
        conn.execute('INSERT INTO settings VALUES (?,?)', ('field', json.dumps(field, ensure_ascii=False)))
        conn.executemany('INSERT INTO entries (kind,value) VALUES (?,?)', [('log', json.dumps(log, ensure_ascii=False)) for log in logs])


def clear_demo():
    with connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='field'").fetchone()
        if not row or not json.loads(row['value']).get('demo'):
            raise ValueError('只允许清除演示田。真实田间记录不会被清除。')
        conn.execute("DELETE FROM settings WHERE key IN ('field','weather')")
        conn.execute('DELETE FROM entries')


def json_pointer(document, pointer):
    """Resolve an RFC 6901 pointer without discarding the original extraction."""
    value = document
    for part in pointer.lstrip('/').split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def paper_file(relative):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError('论文文件必须位于姜小伴文件夹内。')
    return path


def catalog_file(relative, expected_hash):
    """Relocate an unchanged catalog original into papers without guessing its identity."""
    path = paper_file(relative)
    if path.is_file():
        return path
    relocated = paper_file(str(Path('papers') / Path(relative).name))
    if relocated.is_file() and hashlib.sha256(relocated.read_bytes()).hexdigest() == expected_hash:
        return relocated
    return path


def evidence_library():
    """Import traceable pilot cards and locally screened batch candidates."""
    global LIBRARY_CACHE, LIBRARY_SIGNATURE
    with LIBRARY_LOCK:
        try:
            manifest_path = ROOT / 'ginger_evidence_catalog.json'
            try:
                manifest_bytes = manifest_path.read_bytes()
            except FileNotFoundError:
                # The manually prepared pilot catalog is optional for a clean factory run.
                manifest_bytes = b'{"version":"2.0","papers":[]}'
            manifest = json.loads(manifest_bytes)
            package_paths = sorted((ROOT / 'ginger_evidence_results').glob('*.json'),
                                   key=lambda p: p.stat().st_mtime_ns, reverse=True)
            paths = [catalog_file(p[k], p[k + '_sha256']) for p in manifest['papers'] for k in ('pdf', 'json')]
            paths.extend(package_paths)
            paths.append(DATA_DIR / 'evidence_withdrawals.json')
            paths.append(DATA_DIR / 'factory_release_control.json')
            signature = (hashlib.sha256(manifest_bytes).hexdigest(), tuple(
                (str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.is_file() else (str(p), None, None)
                for p in paths))
            if (LIBRARY_CACHE is not None and signature == LIBRARY_SIGNATURE
                    and (DATA_DIR / 'paper_library.sqlite3').is_file()):
                return copy.deepcopy(LIBRARY_CACHE)
            papers, records, warnings = [], [], []
            included_pdf_hashes = set()
            for spec in manifest['papers']:
                try:
                    source_paths = {k: catalog_file(spec[k], spec[k + '_sha256']) for k in ('pdf', 'json')}
                    blobs = {k: source_paths[k].read_bytes() for k in ('pdf', 'json')}
                    for kind, blob in blobs.items():
                        if hashlib.sha256(blob).hexdigest() != spec[kind + '_sha256']:
                            raise ValueError('原文件已变化，需要重新核对后才能引用')
                    document = json.loads(blobs['json'])
                    paper = {k: spec[k] for k in ('id', 'title', 'year', 'scope', 'pdf', 'json', 'pdf_sha256', 'json_sha256')}
                    paper.update({k: source_paths[k].relative_to(ROOT).as_posix() for k in ('pdf', 'json')})
                    selected = []
                    for card in spec['evidence']:
                        if not re.fullmatch(r'[A-Z]{2}\d{2}', card['id']):
                            raise ValueError('证据编号格式不正确')
                        record = {**copy.deepcopy(card), 'paper_id': paper['id'], 'paper_title': paper['title'],
                                  'year': paper['year'], 'scope': paper['scope'],
                                  'pdf_sha256': paper['pdf_sha256'], 'json_sha256': paper['json_sha256'],
                                  'review_status': '关键片段经 AI 对照原文核查，未经农学专家审定',
                                  'extracted_values': {p: json_pointer(document, p) for p in card['json_pointers']}}
                        record['pdf_url'] = f"/papers/{paper['id']}.pdf?v={paper['pdf_sha256']}#page={card['pages'][0]}"
                        record['json_url'] = f"/papers/{paper['id']}.json?v={paper['json_sha256']}"
                        selected.append(record)
                    papers.append(paper)
                    records.extend(selected)
                    included_pdf_hashes.add(paper['pdf_sha256'])
                except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                    warnings.append(f"《{spec.get('title', '论文')}》未接入：{exc}。")
            # A completed scientific review supersedes older candidates, including a
            # genuine zero-result review. A transport/account failure does not.
            seed_papers = {paper['pdf_sha256']: paper for paper in papers}
            latest_reviews = {}
            for package_path in package_paths:
                try:
                    if package_path.stat().st_size > 24 * 1024 * 1024:
                        continue
                    metadata = json.loads(package_path.read_bytes())
                    fingerprint = metadata.get('source_pdf', {}).get('sha256')
                    if metadata.get('evidence_package_version') in ('1.7', '1.8', '1.9', '2.0') and fingerprint:
                        review = metadata.get('agent_evidence', {}).get('independent_review', {})
                        if review.get('status') == 'succeeded' and review.get('source_pdf_sha256') == fingerprint:
                            latest_reviews.setdefault(fingerprint, package_path)
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
            for package_path in package_paths:
                try:
                    if package_path.stat().st_size == 0:
                        # The factory reserves a name before the model finishes.
                        continue
                    package_bytes = package_path.read_bytes()
                    if len(package_bytes) > 24 * 1024 * 1024:
                        warnings.append(f"《{package_path.stem}》证据包超过24 MiB，已跳过。")
                        continue
                    safe_package_path = paper_file(str(package_path.relative_to(ROOT)))
                    if safe_package_path != package_path.resolve():
                        warnings.append(f"《{package_path.stem}》引用了项目外部路径，已跳过。")
                        continue
                    document = json.loads(package_bytes)
                    card_set = document.get('agent_evidence', {})
                    source = document.get('source_pdf', {})
                    source_hash = source.get('sha256')
                    automatic = document.get('evidence_package_version') in ('1.7', '1.8', '1.9', '2.0')
                    if (not isinstance(card_set, dict)
                            or document.get('evidence_package_version') not in ('1.6', '1.7', '1.8', '1.9', '2.0')
                            or (not automatic and (card_set.get('status') != 'generated_unreviewed'
                                or document.get('run_metadata', {}).get('transport_complete') is not True))
                            or not isinstance(source_hash, str)
                            or not re.fullmatch(r'[a-f0-9]{64}', source_hash)):
                        continue
                    if source_hash in latest_reviews and package_path != latest_reviews[source_hash]:
                        continue
                    if source_hash in included_pdf_hashes:
                        # The hand-checked seed cards remain authoritative for those exact PDFs.
                        if not (automatic and source_hash in seed_papers):
                            continue
                    review = card_set.get('independent_review', {})
                    if automatic and (card_set.get('status') != 'automatically_reviewed'
                            or review.get('status') != 'succeeded'
                            or review.get('source_pdf_sha256') != source_hash):
                        warnings.append(f"《{package_path.stem}》自动原文复核未完成，候选已隔离。")
                        continue
                    training = document.get('training_record')
                    if not isinstance(training, dict) or not training:
                        warnings.append(f"《{package_path.stem}》没有可用的训练记录，已跳过。")
                        continue
                    source_name = Path(str(source.get('filename', ''))).name
                    if not source_name or source_name in ('.', '..'):
                        warnings.append(f"《{package_path.stem}》缺少原论文文件名，已跳过。")
                        continue
                    pdf_candidates = []
                    workspace_path = source.get('workspace_relative_path')
                    if isinstance(workspace_path, str):
                        pdf_candidates.append(workspace_path)
                    pdf_candidates.extend((source_name, str(Path('papers') / source_name),
                                           str(Path('ginger_evidence_results') / source_name)))
                    pdf_relative = None
                    for candidate in dict.fromkeys(pdf_candidates):
                        try:
                            candidate_pdf = paper_file(candidate)
                            if (candidate_pdf.is_file()
                                    and hashlib.sha256(candidate_pdf.read_bytes()).hexdigest() == source_hash):
                                pdf_relative = candidate
                                break
                        except (OSError, ValueError):
                            continue
                    bib = {}
                    if automatic and not pdf_relative:
                        warnings.append(f"《{package_path.stem}》原PDF缺失或指纹不符，自动证据暂不引用。")
                        continue
                    for key in ('paper_identity', 'bibliographic_identity', 'paper_metadata'):
                        value = training.get(key)
                        if isinstance(value, dict):
                            bib = value
                            break
                    title = next((bib.get(key) for key in ('title_cn', 'title_zh', 'title', 'title_en')
                                  if isinstance(bib.get(key), str) and bib[key].strip()), Path(source_name).stem)
                    year_value = next((bib.get(key) for key in ('year', 'publication_year', 'pub_year')
                                       if str(bib.get(key, '')).isdigit()), None)
                    publication = bib.get('publication_metadata', {})
                    if not year_value and isinstance(publication, dict):
                        year_value = publication.get('year')
                    archive_id = 'AUTO-' + source_hash[:12].upper()
                    paper_id = seed_papers.get(source_hash, {}).get('id', archive_id)
                    json_relative = str(package_path.relative_to(ROOT))
                    package_hash = hashlib.sha256(package_bytes).hexdigest()
                    pages_count = source.get('page_count')
                    paper = {'id': paper_id, 'title': title, 'year': year_value,
                             'scope': '由论文训练记录生成的检索候选；适用范围以每条证据卡为准。',
                             'pdf': pdf_relative, 'json': json_relative,
                             'pdf_sha256': source_hash if pdf_relative else None,
                             'json_sha256': package_hash, 'pdf_available': bool(pdf_relative)}
                    accepted = 0
                    quality_excluded = 0
                    not_selected = 0
                    publication_hold=automatic_publication_hold(document)
                    for index, card in enumerate(card_set.get('records', []), 1):
                        if (not isinstance(card, dict)
                                or not isinstance(card.get('id'), str)
                                or not re.fullmatch(r'AE-[A-F0-9]{12}-\d{3,}', card['id'])):
                            continue
                        quality = card.get('quality_check')
                        release = card.get('release_decision', {})
                        if publication_hold:
                            quality_excluded += 1
                            continue
                        if document.get('evidence_package_version') == '2.0' and (
                                not independent_gate_is_valid(card)
                                or card['independent_gate'].get('source_pdf_sha256') != source_hash
                                or release.get('status') == 'auto_ready' and card['independent_gate'].get('status') != 'verified'):
                            quality_excluded += 1
                            continue
                        if not protocol_fact_bindings_valid(card, document):
                            quality_excluded += 1
                            continue
                        if automatic and not image_sources_match_pdf(card, paper_file(pdf_relative), source_hash):
                            quality_excluded += 1
                            continue
                        if evidence_is_withdrawn(card, source_hash):
                            quality_excluded += 1
                            continue
                        if automatic and (evidence_content_issue(card) or derivation_issue(card) or measurement_qualifier_issue(card)):
                            quality_excluded += 1
                            continue
                        if automatic and historical_standard_claim(card) and release.get('status') == 'auto_ready':
                            release = {**release, 'status': 'auto_reference', 'eligible_for_training': False,
                                       'reason': '标准或限值仅为论文引用，未核验是否为现行当地标准；' + release.get('reason', '')}
                        if automatic and (release.get('status') not in ('auto_ready', 'auto_reference')
                                or release.get('eligible_for_agent') is not True):
                            quality_excluded += 1
                            continue
                        if not isinstance(quality, dict) or quality.get('status') != 'accepted':
                            quality_excluded += 1
                            continue
                        if quality.get('library_selected') is not True:
                            not_selected += 1
                            continue
                        pointers = card.get('source_pointers')
                        pages = quality.get('resolved_page_numbers')
                        if (not isinstance(pointers, list) or not pointers
                                or not card.get('protocol_policy') and len(pointers)>3
                                or any(not isinstance(p, str) or not p.startswith('/raw_extraction/')
                                       for p in pointers)
                                or not isinstance(pages, list)
                                or any(type(page) is not int or page < 1
                                       or not isinstance(pages_count, int) or page > pages_count
                                       for page in pages)):
                            continue
                        try:
                            values = {pointer: json_pointer(document, pointer) for pointer in pointers}
                        except (KeyError, IndexError, TypeError, ValueError):
                            continue
                        record_id = card['id']
                        if record_id in {r['id'] for r in records}:
                            continue
                        page_fragment = f"#page={pages[0]}" if pages else ''
                        pdf_url = (f"/papers/{paper_id}.pdf?v={source_hash}{page_fragment}"
                                   if pdf_relative else None)
                        records.append({
                            'id': record_id, 'title': '{} · {}'.format({
                                'measured_result': '研究结果',
                                'method': '试验方法',
                                'author_interpretation': '作者解释',
                                'limitation': '研究局限',
                            }.get(card.get('evidence_type'), '论文证据'),
                                '、'.join(k for k in card.get('keywords', [])
                                          if isinstance(k, str))[:36]),
                            'keywords': [k for k in card.get('keywords', []) if isinstance(k, str)][:12],
                            'summary': str(card.get('claim', ''))[:2400],
                            'limitations': str(card.get('limitations', ''))[:1000],
                            'pages': pages, 'page_count': pages_count,
                            'scope': str(card.get('scope', '试验适用范围请查阅原文。'))[:1000],
                            'paper_id': paper_id, 'paper_title': title, 'year': year_value,
                            'pdf_sha256': source_hash, 'json_sha256': package_hash,
                            'review_status': 'AI批量生成的候选证据，未逐篇与PDF核读；仅校验了JSON出处和页码范围。',
                            'release_status': release.get('status') if automatic else 'legacy_reference',
                            'review_notes': [
                                '批量机器整理，不代表农学专家背书或已验证本地适用性。',
                                ('具体数值已在PDF文字中定位。' if quality.get('page_status') == 'verified_text'
                                 else '页码已按PDF文字修正。' if quality.get('page_status') == 'corrected_by_text'
                                 else '该页的具体数值尚未由PDF文字自动核对。'),
                            ],
                            'source_excerpt': str(card.get('source_quote', ''))[:1200] if automatic else '',
                            'json_pointers': pointers, 'extracted_values': values,
                            'pdf_url': pdf_url,
                            'json_url': f"/papers/{archive_id}.json?v={package_hash}",
                            'package_path': json_relative,
                            'keywords_source': 'AI为检索补充的普通提问关键词',
                        })
                        record = records[-1]
                        if automatic:
                            record['review_status'] = ('自动对照原PDF复核并匹配原文引文，已自动放行；未经农学专家审定。'
                                if release['status'] == 'auto_ready' else
                                '自动对照原PDF复核，仅供参考；支撑或适用条件有限，不进入训练语料。')
                            record['review_notes'] = [release.get('reason', ''),
                                '程序自动分级，无需逐篇人工放行；研究试验条件仍需与本田比较。']
                            if any(a.get('kind') == 'page_image' for a in card.get('evidence_anchors', [])):
                                record['source_excerpt'] = '【AI读图摘录，以原PDF页面为准】' + record['source_excerpt']
                                if release['status'] == 'auto_ready':
                                    record['review_status'] = ('Protocol 原文复核通过；参考集尚未验收，仅供检索参考，未作为训练数据。'
                                        if card.get('protocol_policy') else
                                        '两次独立AI核验通过，原页图片指纹已验证；未经农学专家审定。')
                            record['review_notes'].extend(
                                '原文冲突记录：' + (str(c.get('description', '')) if isinstance(c, dict) else str(c))
                                for c in card.get('source_conflicts', []))
                        accepted += 1
                    if accepted:
                        if source_hash not in seed_papers:
                            papers.append(paper)
                        included_pdf_hashes.add(source_hash)
                    elif card_set.get('records'):
                        warnings.append(f"《{title}》证据卡没有通过本机出处检查，已跳过。")
                    if quality_excluded:
                        warnings.append(f"《{title}》有{quality_excluded}条候选卡未通过自动质量检查，未提供给姜小伴引用。")
                    if not_selected:
                        warnings.append(f"《{title}》另有{not_selected}条旧候选卡未选用，可批量自动复核后重新决定。")
                except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                    warnings.append(f"《{package_path.stem}》自动证据未接入：{exc}。")
            if len({r['id'] for r in records}) != len(records):
                raise ValueError('证据编号重复')
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            # A separate, rebuildable database never changes field records or originals.
            with closing(sqlite3.connect(DATA_DIR / 'paper_library.sqlite3', timeout=20)) as conn, conn:
                conn.execute('CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
                conn.execute('CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, value TEXT NOT NULL)')
                conn.execute('DELETE FROM papers')
                conn.execute('DELETE FROM evidence')
                conn.executemany('INSERT INTO papers VALUES (?,?)', [(p['id'], json.dumps(p, ensure_ascii=False)) for p in papers])
                conn.executemany('INSERT INTO evidence VALUES (?,?,?)', [(r['id'], r['paper_id'], json.dumps(r, ensure_ascii=False)) for r in records])
                records = [json.loads(row[0]) for row in conn.execute('SELECT value FROM evidence ORDER BY id')]
            LIBRARY_CACHE = {'papers': papers, 'evidence': records, 'warnings': warnings, 'version': manifest['version']}
            LIBRARY_SIGNATURE = signature
            return copy.deepcopy(LIBRARY_CACHE)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            # Fail closed rather than serve stale or untraceable evidence.
            LIBRARY_CACHE = LIBRARY_SIGNATURE = None
            return {'papers': [], 'evidence': [], 'warnings': ['论文库暂不可用，请检查论文、JSON 和证据目录文件。'], 'version': None}


def library_status():
    library = evidence_library()
    return {'paper_count': len(library['papers']), 'evidence_count': len(library['evidence']),
            'warnings': library['warnings'], 'version': library['version']}


def search_evidence(question, field=None, history=None, limit=6):
    """Transparent topic retrieval for the pilot; no embedding subscription required."""
    library = evidence_library()
    query = question.lower().strip()
    records = library['evidence']
    topic_vocabulary = (
        '产量', '连作', '重茬', '姜瘟', '病害', '菌肥', '有机肥', '施肥',
        '滴灌', '灌溉', '浇水', '土壤', '全氮', '水解性氮', '有效磷',
        '有效钾', '交换性钙', '交换性镁', '重金属', '收获', '采收',
        '老姜', '嫩姜', '干燥', '品质', '生育期', '安全间隔', '用药', '农药',
    )
    topics = [term for term in topic_vocabulary if term in query]
    topics = [term for term in topics if not any(term != other and term in other for other in topics)]
    topic_aliases = {'重茬': ('重茬', '连作'), '连作': ('连作', '重茬'),
                     '菌肥': ('菌肥', '菌剂', '微生物肥')}
    scores = {}
    topic_hits = {}
    for record in records:
        exact = sum(min(len(term), 5) for term in record['keywords'] if term.lower() in query)
        searchable = (record['summary'] + ' ' + ' '.join(record['keywords'])).lower()
        topic_hits[record['id']] = sum(any(alias in searchable for alias in topic_aliases.get(term, (term,)))
                                       for term in topics)
        scores[record['id']] = exact + 6 * topic_hits[record['id']]
    # Short follow-up questions can reuse the immediately preceding cited evidence.
    if not any(scores.values()) and history and len(query) <= 100 and re.search(r'这|那|刚才|上面|依据|原文|为什么|来源', query):
        recent = next((m for m in reversed(history) if m.get('role') == 'assistant'), {})
        ids = {r['id'] for r in recent.get('sources', [])}
        scores = {r['id']: (1 if r['id'] in ids else 0) for r in records}
    broad = not any(scores.values()) and any(t in query for t in ('论文', '文献', '研究', '数据库'))
    if broad:
        # Show one entry per paper first; avoid silently privileging a single study.
        seen = set()
        for r in records:
            scores[r['id']] = 2 if r['paper_id'] not in seen else 1
            seen.add(r['paper_id'])
    local = ' '.join(str((field or {}).get(k, '')) for k in ('address', 'variety', 'harvest_goal'))
    minimum_topics = 2 if len(topics) >= 2 and max(topic_hits.values(), default=0) >= 2 else 0
    ranked = [(score + (0.2 if any(t in local for t in r.get('region_terms', [])) else 0), r)
              for r in records if (score := scores[r['id']]) > 0
              and topic_hits[r['id']] >= minimum_topics]
    ranked.sort(key=lambda pair: (-pair[0], pair[1]['id']))
    # Show up to two cards from each matching paper before filling remaining slots.
    # A paper with many automatically generated cards should not hide other studies.
    by_paper = {}
    for _, record in ranked:
        by_paper.setdefault(record['paper_id'], []).append(record)
    selected = []
    chosen = set()
    for _ in range(2):
        for paper_records in by_paper.values():
            if paper_records and len(selected) < limit:
                record = paper_records.pop(0)
                selected.append(record)
                chosen.add(record['id'])
    if len(selected) < limit:
        selected.extend([record for _, record in ranked
                         if record['id'] not in chosen][:limit - len(selected)])
    return {'query': question, 'results': selected, 'warnings': library['warnings'],
            'paper_count': len(library['papers']), 'evidence_count': len(records),
            'method': '本地关键词与同义词检索；地域只用于相关结果排序，尚未自动判定农学适用性'}
