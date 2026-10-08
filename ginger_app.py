"""姜小伴：run this file, or double-click 启动姜小伴.vbs. Python 3.10+."""
import argparse
import copy
import hashlib
import json
import mimetypes
import re
import secrets
import threading
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ginger_agronomy as agronomy
import ginger_services as services
import ginger_store as store

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
APP_ID = 'ginger-companion-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
AI_LOCK = threading.Lock()
FIELD_LOCK = threading.RLock()
KINDS = ('看看苗', '浇水', '施肥', '用药', '测土', '测产', '采收', '其他')


def clean_text(value, limit=1000):
    return str(value or '').strip()[:limit]


def save_field(data):
    with FIELD_LOCK:
        existing = store.get('field') or {}
        field = {'name': clean_text(data.get('name'), 40) or '我的姜田',
                 'address': clean_text(data.get('address'), 240),
                 'lat': agronomy.number(data.get('lat'), '纬度', -90, 90),
                 'lon': agronomy.number(data.get('lon'), '经度', -180, 180),
                 'location_source': clean_text(data.get('location_source'), 100) or '手动位置（WGS84）',
                 'area': agronomy.number(data.get('area'), '面积（亩）', 0.01, 100000),
                 'planted': agronomy.valid_date(data.get('planted'), '播种日期'),
                 'harvest_goal': data.get('harvest_goal', '老姜'),
                 'variety': clean_text(data.get('variety'), 60),
                 'baseline_yield': agronomy.number(data.get('baseline_yield'), '往年亩产（公斤/亩）', 0, 30000, True),
                 'cycle_days': agronomy.number(data.get('cycle_days'), '当地生育天数', 90, 450, True),
                 'demo': bool(existing.get('demo', False))}
        if not field['address']:
            raise ValueError('请先选择或写下田的位置。')
        if field['harvest_goal'] not in ('老姜', '嫩姜'):
            raise ValueError('请选择嫩姜或老姜。')
        store.put('field', field)
        return field


def save_log(data):
    field = store.get('field')
    if not field:
        raise ValueError('先建好您的姜田，再记一笔。')
    kind = data.get('type', '看看苗')
    if kind not in KINDS:
        raise ValueError('记录类型不正确。')
    item = {'type': kind, 'date': agronomy.valid_date(data.get('date'), '记录日期', False),
            'note': clean_text(data.get('note'), 3000), 'created': datetime.now().astimezone().isoformat(timespec='seconds')}
    if kind == '看看苗':
        item['condition'] = clean_text(data.get('condition'), 30) or '长势正常'
        if item['condition'] not in ('长势正常', '叶子发黄', '有虫或病斑', '积水', '土有点干'):
            raise ValueError('请选择一种长势。')
    if kind in ('施肥', '用药', '浇水', '采收'):
        item.update(product=clean_text(data.get('product'), 100), ingredient=clean_text(data.get('ingredient'), 200),
                    amount=agronomy.number(data.get('amount'), '用量', 0, 1e8, True), unit=clean_text(data.get('unit'), 20) or '公斤',
                    scope=clean_text(data.get('scope'), 20) or '整片田')
        if item['scope'] not in ('整片田', '每亩', '局部区域'):
            raise ValueError('请选择用量对应的范围。')
        if not item['note'] and item['amount'] is None and not item['product']:
            raise ValueError('随便记几个字，或填一下用量，就能保存。')
    if kind == '测土':
        item['soil'] = {key: agronomy.number(data.get(key), label, low, high, True) for key, label, low, high in [
            ('ph', 'pH', 0, 14), ('potassium', '钾', 0, 1e6), ('magnesium', '镁', 0, 1e6),
            ('calcium', '钙', 0, 1e6), ('nitrogen', '氮', 0, 1e6), ('organic', '有机质', 0, 1000)]}
        item['soil_unit'] = clean_text(data.get('soil_unit'), 30) or 'mg/kg（土壤）'
        item['organic_unit'] = clean_text(data.get('organic_unit'), 20) or 'g/kg'
        item['method'] = clean_text(data.get('method'), 240)
        if item['soil_unit'] not in ('mg/kg（土壤）', 'mg/L（溶液）', 'cmol/kg（土壤）', 'mmol/L（溶液）') or item['organic_unit'] not in ('g/kg', '%'):
            raise ValueError('请选择检测报告对应的单位。')
        if item['organic_unit'] == '%' and (item['soil']['organic'] or 0) > 100:
            raise ValueError('有机质百分比不能超过 100%，请检查报告上的单位。')
        if not any(v is not None for v in item['soil'].values()) and not item['note']:
            raise ValueError('有哪一项就填哪一项，至少记一项结果或备注。')
    if kind == '测产':
        item['sample_m2'] = agronomy.number(data.get('sample_m2'), '样方面积（平方米）', 0.01, 100000)
        item['sample_kg'] = agronomy.number(data.get('sample_kg'), '鲜姜重量（公斤）', 0, 1e7)
        item['sample_stage'] = data.get('sample_stage', '生长期')
        if item['sample_stage'] not in ('生长期', '采收期'):
            raise ValueError('请选择测产时期。')
        if item['date'] < field['planted']:
            raise ValueError('测产日期不能早于播种日期。')
    if kind == '其他' and not item['note']:
        raise ValueError('写几个字，留住今天的事情。')
    item['id'] = store.add('log', item)
    return item


def state():
    field, logs = store.get('field'), store.entries('log')
    cached = store.get('weather')
    if cached and field and cached.get('location') != [round(field['lat'], 4), round(field['lon'], 4)]:
        cached = None
    if cached:
        import time
        cached['stale'] = time.time() - cached.get('timestamp', 0) >= 3600
    return {'token': TOKEN, 'field': field, 'logs': logs, 'chat': farmer_payload(list(reversed(store.entries('chat')))),
            'settings': services.settings(), 'weather': cached, 'library': farmer_payload(store.library_status()),
            'estimate': agronomy.estimate(field, logs) if field else None,
            'advice': agronomy.advice(field, logs, cached) if field else []}


def farmer_payload(value):
    """Keep operational diagnostics and raw extraction details in the developer view."""
    internal = {'warnings', 'review_notes', 'review_status', 'json_pointers', 'source_pointers',
                'extracted_values', 'pdf_sha256', 'json_sha256', 'json_url', 'source_excerpt'}
    if isinstance(value, list):
        return [farmer_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: ([] if key == 'warnings' else farmer_payload(item))
                for key, item in value.items() if key not in internal or key == 'warnings'}
    return value


def debug_evidence():
    """Read the latest candidate package for each paper, including withheld cards."""
    library = store.evidence_library()
    run_warnings = []
    admitted = {card['id'] for card in library['evidence']}
    seen, papers = set(), []
    candidates = sorted((ROOT / 'ginger_evidence_results').glob('*.json'),
                        key=lambda item: item.stat().st_mtime_ns, reverse=True)
    for path in candidates:
        try:
            if not path.resolve().is_relative_to(ROOT.resolve()) or not 0 < path.stat().st_size <= 24 * 1024 * 1024:
                continue
            blob = path.read_bytes()
            package = json.loads(blob)
            source = package.get('source_pdf', {})
            fingerprint = source.get('sha256')
            records = package.get('agent_evidence', {}).get('records', [])
            if not fingerprint or fingerprint in seen or not isinstance(records, list):
                continue
            if package.get('agent_evidence', {}).get('independent_review', {}).get('status') != 'succeeded':
                run_warnings.append('最近一次处理未完成，展示上一次完成原文复核的记录：' + Path(source.get('filename', path.stem)).name)
                continue
            seen.add(fingerprint)
            paper_id = 'AUTO-' + fingerprint[:12].upper()
            indexed = next((p for p in library['papers'] if p['pdf_sha256'] == fingerprint), {})
            cards = []
            for raw in records:
                card = copy.deepcopy(raw)
                release = card.get('release_decision', {})
                issue = store.evidence_content_issue(card) or store.derivation_issue(card)
                if issue:
                    card['release_decision'] = {**release, 'status': 'quarantined', 'reason': issue,
                                                'eligible_for_agent': False, 'eligible_for_training': False}
                card['currently_admitted'] = card.get('id') in admitted
                card['pdf_url'] = (f"/papers/{indexed['id']}.pdf?v={fingerprint}" if indexed else None)
                card['json_url'] = f"/papers/{paper_id}.json?v={hashlib.sha256(blob).hexdigest()}"
                cards.append(card)
            papers.append({'id': paper_id, 'title': indexed.get('title') or Path(source.get('filename', path.stem)).stem,
                           'version': package.get('app_version', ''), 'cards': cards})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return {'papers': papers, 'warnings': library['warnings'] + run_warnings}


def demo():
    today = date.today()
    field = {'name': '院后的姜田', 'address': '中国 · 山东省 · 潍坊市 · 安丘市（演示位置）', 'lat': 36.4785, 'lon': 119.218,
             'location_source': '演示位置', 'area': 3.5, 'planted': (today - timedelta(days=162)).isoformat(),
             'harvest_goal': '老姜', 'variety': '当地大姜', 'baseline_yield': 3000, 'cycle_days': 240, 'demo': True}
    logs = [
        {'type': '施肥', 'date': (today-timedelta(days=6)).isoformat(), 'note': '演示：施肥后留意长势。', 'product': '示例复合肥', 'amount': 20, 'unit': '公斤', 'scope': '整片田'},
        {'type': '浇水', 'date': (today-timedelta(days=3)).isoformat(), 'note': '演示：早上浇了一次，沟里没有积水。'},
        {'type': '看看苗', 'date': today.isoformat(), 'note': '演示：叶子舒展，长势正常。', 'condition': '长势正常'},
    ]
    store.seed_demo(field, logs)
    return state()


def chat(data):
    question = clean_text(data.get('question'), 2000)
    if not question:
        raise ValueError('写下您想问的事。')
    if not AI_LOCK.acquire(blocking=False):
        raise ValueError('上一条问题还在回答，稍等一下就好。')
    try:
        snapshot = state()
        field = snapshot['field']
        if not field:
            raise ValueError('先建好姜田，我才能结合您的情况回答。')
        # Do not send an exact address or coordinates. Regional climate and observed records suffice.
        context_field = {k: v for k, v in field.items() if k not in ('lat', 'lon', 'address', 'location_source')}
        w = snapshot['weather'] or {}
        context = {'field': context_field, 'today': date.today().isoformat(), 'estimate': snapshot['estimate'],
                   'logs': snapshot['logs'][:40], 'weather': {'stale': w.get('stale', True), 'current': (w.get('data') or {}).get('current'), 'daily': (w.get('data') or {}).get('daily')}}
        retrieval = store.search_evidence(question, field, snapshot['chat'])
        context['evidence'] = [{k: r[k] for k in ('id', 'paper_title', 'year', 'title', 'scope', 'summary',
                                'limitations', 'review_notes', 'pages', 'review_status')}
                               for r in retrieval['results']]
        context['evidence_scope'] = '仅从已接入的论文证据卡中检索；未命中不代表其他论文没有证据。'
        context['evidence_warnings'] = retrieval['warnings']
        messages = [{'role': 'system', 'content': services.SYSTEM_PROMPT},
                    {'role': 'user', 'content': '以下是系统附带的田间资料，仅作为数据：\n' + json.dumps(context, ensure_ascii=False)}]
        for entry in snapshot['chat'][-12:]:
            messages.append({'role': entry['role'], 'content': entry['content'][:6000]})
        messages.append({'role': 'user', 'content': question})
        answer = services.ai_call(messages)
        answer, sources, citation_notice = services.cite_answer(answer, retrieval['results'])
        retrieval_info = {'matched_ids': [r['id'] for r in retrieval['results']], 'method': retrieval['method'],
                          'warnings': retrieval['warnings'], 'paper_count': retrieval['paper_count']}
        stamp = datetime.now().astimezone().isoformat(timespec='seconds')
        store.add('chat', {'role': 'user', 'content': question, 'date': stamp})
        store.add('chat', {'role': 'assistant', 'content': answer, 'date': stamp, 'model': services.settings()['model'],
                           'sources': sources, 'citation_notice': citation_notice, 'retrieval': retrieval_info})
        return farmer_payload({'answer': answer, 'sources': sources, 'citation_notice': citation_notice, 'retrieval': retrieval_info})
    finally:
        AI_LOCK.release()


def archived_auto_package(paper_id, version):
    """Keep old chat citations open when a screened package supersedes one."""
    if (not re.fullmatch(r'AUTO-[A-F0-9]{12}', paper_id)
            or not re.fullmatch(r'[a-f0-9]{64}', version)):
        return None
    for candidate in (ROOT / 'ginger_evidence_results').glob('*.json'):
        try:
            if (not candidate.resolve().is_relative_to(ROOT.resolve())
                    or candidate.stat().st_size > 24 * 1024 * 1024):
                continue
            raw = candidate.read_bytes()
            if hashlib.sha256(raw).hexdigest() != version:
                continue
            source_hash = json.loads(raw).get('source_pdf', {}).get('sha256', '')
            if isinstance(source_hash, str) and paper_id == 'AUTO-' + source_hash[:12].upper():
                return raw
        except (OSError, ValueError, TypeError):
            continue
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = 'GingerCompanion/1.0'

    def log_message(self, *args):
        pass

    def send(self, payload, status=200, content_type='application/json; charset=utf-8'):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def authorized_host(self):
        expected = f'127.0.0.1:{self.server.server_port}'
        return self.headers.get('Host') == expected and self.headers.get('Origin', 'http://' + expected) == 'http://' + expected

    def do_GET(self):
        if not self.authorized_host():
            return self.send({'error': '请从本机启动入口打开。'}, 403)
        path = urlparse(self.path).path
        if path == '/api/ping':
            return self.send({'app': APP_ID})
        if path == '/api/state':
            return self.send(state())
        if path == '/api/library':
            return self.send(farmer_payload(store.evidence_library()))
        if path == '/api/library-status':
            return self.send(farmer_payload(store.library_status()))
        if path == '/api/debug/evidence':
            return self.send(debug_evidence())
        if path == '/debug/evidence':
            return self.send((ROOT / '本轮证据复核清单_2026-10-04.html').read_bytes(), content_type='text/html; charset=utf-8')
        if path.startswith('/papers/'):
            # Serve indexed originals and immutable prior package versions by hash.
            library = store.evidence_library()
            for paper in library['papers']:
                for kind, mime in (('pdf', 'application/pdf'), ('json', 'application/json; charset=utf-8')):
                    if path == f"/papers/{paper['id']}.{kind}":
                        relative = paper.get(kind)
                        if not isinstance(relative, str):
                            return self.send({'error': '原论文PDF不在项目文件夹内；可打开证据包查来源。'}, 404)
                        version = parse_qs(urlparse(self.path).query).get('v', [''])[0]
                        if version != paper[kind + '_sha256']:
                            if kind == 'json':
                                archived = archived_auto_package(paper['id'], version)
                                if archived is not None:
                                    return self.send(archived, content_type='application/json; charset=utf-8')
                            return self.send({'error': '文件版本与引用不一致，请重新核对来源。'}, 409)
                        try:
                            raw = store.paper_file(paper[kind]).read_bytes()
                            if hashlib.sha256(raw).hexdigest() != version:
                                return self.send({'error': '原文件已变化，请重新核对来源。'}, 409)
                            return self.send(raw, content_type=mime)
                        except OSError:
                            return self.send({'error': '原始文件暂不可用。'}, 404)
            match = re.fullmatch(r'/papers/(AUTO-[A-F0-9]{12})\.json', path)
            if match:
                version = parse_qs(urlparse(self.path).query).get('v', [''])[0]
                archived = archived_auto_package(match.group(1), version)
                if archived is not None:
                    return self.send(archived, content_type='application/json; charset=utf-8')
            return self.send({'error': '未找到经过核对的这份论文版本。'}, 404)
        files = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/field.svg': 'field.svg', '/favicon.svg': 'favicon.svg',
                 '/evidence-review.js': 'evidence-review.js', '/evidence-review.css': 'evidence-review.css'}
        files.update({'/debug/ginger_ui/evidence-review.js': 'evidence-review.js',
                      '/debug/ginger_ui/evidence-review.css': 'evidence-review.css'})
        if path in files:
            file = ROOT / 'ginger_ui' / files[path]
            kind = {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css', 'svg': 'image/svg+xml'}[file.suffix[1:]]
            return self.send(file.read_bytes(), content_type=kind + '; charset=utf-8')
        self.send({'error': '没有找到这个页面。'}, 404)

    def do_POST(self):
        if not self.authorized_host() or not secrets.compare_digest(self.headers.get('X-Ginger-Token', ''), TOKEN):
            return self.send({'error': '页面连接已失效，请刷新页面。'}, 403)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 100_000:
                raise ValueError('内容过长或为空，请分几次记录。')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('提交格式不正确。')
            path = urlparse(self.path).path
            if path == '/api/field':
                result = save_field(data)
            elif path == '/api/log':
                result = save_log(data)
            elif path == '/api/log/delete':
                store.remove('log', int(data['id']))
                result = {'ok': True}
            elif path == '/api/settings':
                result = services.save_settings(data)
            elif path == '/api/geocode':
                result = {'results': services.geocode(data.get('query', ''))}
            elif path == '/api/weather':
                result = services.weather(store.get('field'), bool(data.get('force')))
            elif path == '/api/demo':
                result = demo()
            elif path == '/api/demo/clear':
                if AI_LOCK.locked():
                    raise ValueError('姜小伴正在回答问题，请等回答完成后再清除演示田。')
                store.clear_demo()
                result = {'ok': True}
            elif path == '/api/chat':
                result = chat(data)
            elif path == '/api/evidence/search':
                query = clean_text(data.get('query'), 2000)
                if not query:
                    raise ValueError('写下想查的种植问题，例如“滴灌能少施肥吗”。')
                result = store.search_evidence(query, store.get('field'))
                result = farmer_payload(result)
            elif path == '/api/ai/test':
                if not AI_LOCK.acquire(blocking=False):
                    raise ValueError('AI 正在回答问题，请稍后再测试。')
                try:
                    result = {'message': services.ai_call([{'role': 'user', 'content': '连接测试，请只回复“姜小伴已连接”。'}])}
                finally:
                    AI_LOCK.release()
            elif path == '/api/export':
                result = {'format': 'ginger-companion-v1', 'exported': datetime.now().astimezone().isoformat(), 'field': store.get('field'), 'records': store.entries('log'), 'conversations': store.entries('chat')}
            elif path == '/api/shutdown':
                result = {'ok': True}
                threading.Timer(0.5, self.server.shutdown).start()
            else:
                return self.send({'error': '没有找到这个功能。'}, 404)
            self.send(result)
        except (ValueError, TypeError, KeyError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else '有一项内容不完整，请检查后再保存。'
            self.send({'error': message}, 400)
        except Exception:
            self.send({'error': '这次操作没有完成，请重试。若持续失败，请检查数据文件夹是否可写。'}, 500)


def main():
    parser = argparse.ArgumentParser(description='姜小伴 · 本地种植陪伴')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = None
    for port in range(args.port, args.port + 10):
        try:
            server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            try:
                result = services.fetch_json(f'http://127.0.0.1:{port}/api/ping')
                if result.get('app') == APP_ID:
                    if not args.no_browser:
                        webbrowser.open(f'http://127.0.0.1:{port}')
                    return
            except Exception:
                pass
    if server is None:
        raise RuntimeError('启动端口都被占用，请关闭之前的姜小伴后再试。')
    store.get('field')
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(f'http://127.0.0.1:{server.server_port}')).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import tkinter.messagebox
        tkinter.messagebox.showerror('姜小伴没有启动', str(error))
