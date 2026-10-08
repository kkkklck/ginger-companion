"""Transparent prototype estimates, not a calibrated crop model."""
from datetime import date, timedelta
import math

SOURCES = [
    {'title': 'ICAR-IISR：生姜栽培与成熟期', 'url': 'https://spices.res.in/products/spices/ginger.html'},
    {'title': 'ICAR-IISR：有机栽培手册（成熟 210–240 天、鲜食可较早采收）', 'url': 'https://spices.res.in/storage/app/public/pdfs/GENERAL/3ENG.pdf'},
    {'title': 'Open-Meteo：天气模型和指标说明', 'url': 'https://open-meteo.com/en/docs'},
]


def number(value, label, low, high, optional=False):
    if optional and (value is None or value == ''):
        return None
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{label}请填数字。') from None
    if not math.isfinite(n) or not low <= n <= high:
        raise ValueError(f'{label}请填 {low:g} 到 {high:g} 之间的数字。')
    return n


def valid_date(value, label, future=True):
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f'请选一个正确的{label}。') from None
    if not date(2000, 1, 1) <= d <= date.today() + timedelta(days=730):
        raise ValueError(f'{label}超出可记录的时间范围。')
    if not future and d > date.today():
        raise ValueError(f'{label}不能是未来的日期。')
    return d.isoformat()


def estimate(field, logs, today=None):
    today = today or date.today()
    planted = date.fromisoformat(field['planted'])
    age = (today - planted).days
    young = field.get('harvest_goal') == '嫩姜'
    typical = (150, 190) if young else (210, 270)
    custom = field.get('cycle_days')
    window = (max(90, int(custom) - 15), int(custom) + 15) if custom else typical
    start, end = [planted + timedelta(days=n) for n in window]
    stage = '准备种下' if age < 0 else ('发芽扎根' if age < 45 else '长苗分蘖' if age < 100 else '姜块长大' if age < window[0] else '留意采收')
    basis = '按播种日期和采收用途推算；品种、当地温度、霜期及实际长势还会改变日期。'
    if custom:
        basis = '按您填写的当地生育天数，前后各留 15 天；这不是气象积温模型。'
    # Never extrapolate an immature sample to final harvest without a validated model.
    samples = [r for r in logs if r.get('type') == '测产' and r.get('sample_m2') and r.get('sample_kg') is not None and r.get('sample_stage') == '采收期']
    samples = [r for r in samples if planted <= date.fromisoformat(r['date']) <= today]
    samples.sort(key=lambda r: (r['date'], r.get('id', 0)), reverse=True)
    baseline = field.get('baseline_yield')
    if samples:
        # Only combine the latest sampling day: different dates are different crop states.
        latest = samples[0]['date']
        chosen = [r for r in samples if r['date'] == latest]
        center = sum(r['sample_kg'] for r in chosen) / sum(r['sample_m2'] for r in chosen) * (2000 / 3)
        spread = 0.25
        ybasis = f'按 {latest} 采收期样方总重量 ÷ 总面积 × 666.67 平方米/亩；上下留 25% 展示抽样误差情景，非统计置信区间。请选田中多处代表性位置，别只挑长得好的。'
        label = '采收样方估算'
    elif baseline is not None:
        center, spread = baseline, 0.35
        ybasis = '以您填写的往年或邻近同品种亩产为基准，上下留 35% 的情景范围；尚未用本田采收样方校准。'
        label = '历史基准估算'
    else:
        center = None
        spread = 0
        ybasis = '还缺本地亩产基准。记下往年每亩收多少，或临近采收时称几处样方，才能给出有依据的数字。'
        label = '等一份本地依据'
    low = round(center * (1 - spread)) if center is not None else None
    high = round(center * (1 + spread)) if center is not None else None
    return {'age': age, 'stage': stage, 'progress': max(0, min(100, round(age / sum(window) * 200))),
            'harvest_start': start.isoformat(), 'harvest_end': end.isoformat(), 'harvest_basis': basis,
            'overdue': today > end, 'yield_low': low, 'yield_high': high,
            'total_low': round(low * field['area']) if low is not None else None,
            'total_high': round(high * field['area']) if high is not None else None,
            'yield_basis': ybasis, 'yield_label': label, 'confidence': '尚未校准',
            'sources': SOURCES}


def advice(field, logs, weather, today=None):
    today = today or date.today()
    e = estimate(field, logs, today)
    tips = []
    current = (weather or {}).get('data') or {}
    # Stale cached weather is displayed but must not drive today's actions.
    daily = current.get('daily', {}) if not (weather or {}).get('stale') else {}
    days = daily.get('time', [])
    index = days.index(today.isoformat()) if today.isoformat() in days else None
    def datum(key):
        values = daily.get(key, [])
        return values[index] if index is not None and len(values) > index else None
    rain, high, low = datum('precipitation_sum'), datum('temperature_2m_max'), datum('temperature_2m_min')
    if rain is not None and rain >= 15:
        tips.append({'tone': 'amber', 'title': '先看看排水沟', 'body': f'今天预报约 {rain:g} 毫米降水。巡一圈排水口，若田里积水，先疏通；施肥和浇水等看过土再定。', 'tag': '根据天气'})
    if high is not None and high >= 33:
        tips.append({'tone': 'amber', 'title': '午后热，早晚去看苗', 'body': '留意叶片萎蔫和表土干湿。先查看根区土壤，再决定是否补水；空气湿度不等于土壤水分。', 'tag': '根据天气'})
    if low is not None and low <= 10:
        tips.append({'tone': 'amber', 'title': '天转凉，留意保温', 'body': '低温可能影响生长。查看当地寒潮预警，接近成熟时与当地农技员核对采收安排。', 'tag': '根据天气'})
    recent = [r for r in logs if 0 <= (today - date.fromisoformat(r['date'])).days <= 7]
    if any(r.get('condition') in ('叶子发黄', '有虫或病斑', '积水') for r in recent):
        tips.append({'tone': 'amber', 'title': '再看看上次不对劲的地方', 'body': '您最近记过异常。看看有没有扩大，留意根部和排水，记录发生位置。仅凭叶黄不能确定缺肥或病害，先别急着叠加用药。', 'tag': '根据您的记录'})
    if e['age'] < 0:
        tips.append({'tone': 'green', 'title': '种下之前，先把排水做好', 'body': '确认种姜健康、田块排水通畅，并与当地农技员核对适宜播期。种下后改一下实际日期就行。', 'tag': '播种准备'})
    elif e['overdue']:
        tips.append({'tone': 'amber', 'title': '参考采收窗口已经过去', 'body': '请确认是否已经采收。若还在田里，结合实际成熟度、霜期和销售用途重新核对日期，不再按旧窗口倒计时。', 'tag': '采收提醒'})
    else:
        tips.append({'tone': 'green', 'title': '今天花几分钟，走一圈田', 'body': '看看叶色、沟里有没有积水，再摸摸根区土壤。长势正常也可以记一笔，日后回看更有把握。', 'tag': e['stage']})
    if not tips or len(tips) < 2:
        tips.append({'tone': 'green', 'title': '肥和药，记下就不容易忘', 'body': '用过什么、哪天用的、用了多少，随手留一笔。商品名不能代替有效成分，暂不根据品牌自动开用量。', 'tag': '田间小习惯'})
    return tips[:3]
