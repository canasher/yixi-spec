#!/usr/bin/env python3
"""只读检查规范、中文路径、追踪与算例；不访问网络，不代替业务验收。"""
from __future__ import annotations
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import hashlib
import json
import re
import sys
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[1]
BUSINESS = ROOT / '业务' / '租洗'
SPECS = BUSINESS / '共用规范'

def load(name: str):
    return json.loads((BUSINESS / name).read_text(encoding='utf-8'))

def cents(value: str) -> int:
    """测试金额只接受精确到分的非负十进制字符串。"""
    assert isinstance(value, str), '金额必须为字符串'
    amount = Decimal(value) * 100
    assert amount.is_finite() and amount >= 0 and amount == amount.to_integral_value()
    return int(amount)

def check_revision_scenarios(fixture: dict, uat_ids: set[str]) -> int:
    """验证R1.1文档算例和守恒；不模拟数据库锁或宣称业务验收通过。"""
    scenarios = fixture['revisionScenarios']
    count = 0
    all_ids = set()
    for group in ('fulfillment', 'refundLimits', 'creditRefunds', 'compensation'):
        for case in scenarios[group]:
            assert case['id'] not in all_ids, '修订算例ID重复'
            all_ids.add(case['id'])
            assert case['uat'] in uat_ids, '修订算例缺少UAT来源'
            count += 1

    for case in scenarios['fulfillment']:
        state = dict(case['initial'])
        applied = {}
        for step in case['steps']:
            op, quantity, source = step['operation'], step['quantity'], step['source']
            assert type(quantity) is int and quantity > 0
            if source in applied:
                assert applied[source] == (op, quantity), '同来源不同内容'
            else:
                if op in ('return_keep', 'return_cancel'):
                    assert state['I'] >= quantity and state['O'] >= quantity
                    state['I'] -= quantity
                    state['O'] -= quantity
                    state['R' if op == 'return_keep' else 'C'] += quantity
                elif op == 'load':
                    assert state['R'] >= quantity
                    state['R'] -= quantity
                    state['O'] += quantity
                    state['I'] += quantity
                elif op == 'deliver':
                    assert state['I'] >= quantity
                    state['I'] -= quantity
                    state['F'] += quantity
                elif op == 'cancel_reserved':
                    assert state['R'] >= quantity
                    state['R'] -= quantity
                    state['C'] += quantity
                else:
                    raise AssertionError(f'未知履约动作：{op}')
                applied[source] = (op, quantity)
            state['A'] = state['Q'] + state['T'] - state['R'] - state['O']
            assert state['D'] == sum(state[k] for k in ('C', 'F', 'I', 'R'))
            assert all(state[k] >= 0 for k in ('R', 'O', 'C', 'F', 'I'))
            assert all(state[k] == v for k, v in step['expected'].items()), case['id']

    for case in scenarios['refundLimits']:
        paid, approved = cents(case['paid']), cents(case['approved'])
        refunded, held = cents(case['refunded']), cents(case['held'])
        source_refunded = cents(case.get('sourceRefunded', case['refunded']))
        source_held = cents(case.get('sourceHeld', case['held']))
        for step in case['steps']:
            amount, action = cents(step['amount']), step['action']
            assert amount > 0
            if action in ('hold', 'succeed'):
                accepted = amount <= min(paid - refunded - held, approved - source_refunded - source_held)
                if accepted:
                    if action == 'hold':
                        held += amount
                        source_held += amount
                    else:
                        refunded += amount
                        source_refunded += amount
            elif action in ('unknown', 'confirmed_failure', 'settle_hold'):
                accepted = amount <= min(held, source_held)
                if accepted and action != 'unknown':
                    held -= amount
                    source_held -= amount
                    if action == 'settle_hold':
                        refunded += amount
                        source_refunded += amount
            else:
                raise AssertionError(f'未知退款动作：{action}')
            assert accepted == step['accepted'], case['id']
            assert refunded == cents(step['expectedRefunded']), case['id']
            assert held == cents(step['expectedHeld']), case['id']
            assert 0 <= refunded + held <= paid
            assert 0 <= source_refunded + source_held <= approved

    for case in scenarios['creditRefunds']:
        paid, repaid = cents(case['creditPaid']), cents(case['repaid'])
        released, returned = cents(case['priorDebtRelease']), cents(case['priorRepaidReturn'])
        amount = cents(case['refund'])
        debt, repaid_remaining = paid - repaid - released, repaid - returned
        held_debt = cents(case.get('heldDebtRelease', '0.00'))
        held_repaid = cents(case.get('heldRepaidReturn', '0.00'))
        assert debt >= held_debt and repaid_remaining >= held_repaid
        assert 0 < amount <= paid - released - returned - held_debt - held_repaid
        debt_release = min(amount, debt - held_debt)
        repaid_return = amount - debt_release
        assert repaid_return <= repaid_remaining - held_repaid
        actual = (debt_release, repaid_return, debt - debt_release, repaid_remaining - repaid_return)
        expected = tuple(cents(case[k]) for k in ('expectedDebtRelease', 'expectedRepaidReturn', 'expectedDebt', 'expectedRepaidRemaining'))
        assert actual == expected, case['id']
        assert sum(actual[2:]) == paid - released - returned - amount

    for case in scenarios['compensation']:
        basis, settled = cents(case['basis']), cents(case['settled'])
        rate = Decimal(case['rate'])
        assert 0 <= settled <= basis and rate.is_finite() and 0 <= rate <= 100
        unpaid = basis - settled
        if case['basisKind'] == 'ASSESSED_AMOUNT_PERCENT':
            adjustment = int((Decimal(basis) * rate / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        elif case['basisKind'] == 'PAID_AMOUNT_PERCENT':
            adjustment = unpaid + int((Decimal(settled) * rate / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        else:
            raise AssertionError('未知赔偿减免策略')
        offset = min(adjustment, unpaid)
        refund = adjustment - offset
        actual = (adjustment, offset, refund, unpaid - offset)
        expected = tuple(cents(case[k]) for k in ('expectedAdjustment', 'expectedOffset', 'expectedRefund', 'expectedDue'))
        assert actual == expected, case['id']
        assert 0 <= refund <= settled and adjustment == offset + refund
        assert basis - adjustment == settled - refund + unpaid - offset
    return count

def check_text_sync(registries: dict) -> None:
    """主版与机器可读卡片逐字段核对，避免只验证编号存在。"""
    specs = (
        ('BR-R', '业务规则.md', {'pre': '前置条件', 'effect': '生效动作/数据变化', 'retry': '重复与并发', 'reverse': '撤销/更正', 'example': '算例', 'config': '灵活性与限制'}),
        ('TASK-', '实施计划.md', {'deps': '前置依赖', 'deliver': '本任务交付', 'notdo': '明确不做', 'gate': '采纳/环境门槛', 'verify': '必须验证', 'uat': '验收用例'}),
        ('UAT-R', '验收用例.md', {'gate': '执行前提', 'pre': '初始数据', 'steps': '操作步骤', 'expected': '预期结果'}),
    )
    for prefix, name, mapping in specs:
        content = (SPECS / name).read_text(encoding='utf-8')
        for entry in registries[prefix]:
            block = re.search(r'### ' + re.escape(entry['id']) + r' .*?(?=\n<a id=|\n## |\Z)', content, re.S)
            assert block, f'缺少主版卡片：{entry["id"]}'
            for key, label in mapping.items():
                row = re.search(r'\| ' + re.escape(label) + r' \| (.*?) \|', block.group())
                assert row and row.group(1) == entry[key], f'主版/JSON不一致：{entry["id"]}/{key}'
            if prefix == 'TASK-':
                assert f"| 需求/规则 | {entry['fr']} / {entry['br']} |" in block.group(), entry['id']
            elif prefix == 'UAT-R':
                assert f"| 类别/规则 | {entry['kind']} / {entry['br']} |" in block.group(), entry['id']
                assert f"| 当前执行结果 | {entry['status']}" in block.group(), entry['id']
            else:
                assert f"**来源输入：** {entry['source']}。 **决策：** {entry['decision']}。" in block.group(), f"来源/决策不同步：{entry['id']}"

    product = (SPECS / '产品需求.md').read_text(encoding='utf-8')
    for entry in registries['FR-R']:
        row = f"| {entry['id']} | {entry['name']} | {entry['scope']} | {entry['definition']} | {entry['rules']} / {entry['tasks']} |"
        assert row in product, f"需求主版/JSON不一致：{entry['id']}"
    decisions = (SPECS / '决策记录.md').read_text(encoding='utf-8')
    for entry in registries['PD-']:
        row = f"| {entry['id']} {entry['title']} | {entry['source']} | {entry['recommended']} | {entry['gate']}；{entry['status']} |"
        assert row in decisions, f"决策主版/JSON不一致：{entry['id']}"

def identifier_refs(value: str, prefix: str) -> set[str]:
    """接受完整编号，以及既有中文范围和斜线简写。"""
    width = 2 if prefix == 'TASK-' else 3
    escaped = re.escape(prefix)
    number = rf'\d{{{width}}}'
    refs = set(re.findall(escaped + number + r'(?!\d)', value))
    for start, end in re.findall(escaped + f'({number})[～~–](?:{escaped})?({number})', value):
        assert int(start) <= int(end), f'编号范围倒置：{prefix}{start}～{end}'
        refs.update(f'{prefix}{n:0{width}d}' for n in range(int(start), int(end) + 1))
    for first, rest in re.findall(escaped + f'({number})((?:/{number})+)', value):
        refs.update(prefix + n for n in (first, *rest[1:].split('/')))
    return refs

def check_relations(registries: dict, features: list[dict]) -> None:
    ids = {prefix: {e['id'] for e in entries} for prefix, entries in registries.items()}
    fields = {
        'FR-R': {'rules': 'BR-R', 'tasks': 'TASK-', 'uat': 'UAT-R'},
        'BR-R': {'decision': 'PD-'},
        'TASK-': {'deps': 'TASK-', 'fr': 'FR-R', 'br': 'BR-R', 'uat': 'UAT-R'},
        'UAT-R': {'br': 'BR-R'},
    }
    for prefix, mapping in fields.items():
        for entry in registries[prefix]:
            for field, target in mapping.items():
                missing = identifier_refs(entry[field], target) - ids[target]
                assert not missing, f"{entry['id']}/{field}引用不存在：{missing}"
    for feature in features:
        for field, prefix in (('requirement', 'FR-R'), ('rules', 'BR-R'), ('tasks', 'TASK-'), ('uat', 'UAT-R')):
            refs = identifier_refs(feature[field], prefix)
            assert not refs - ids[prefix], f"范围追踪引用无效：{feature['id']}/{field}"
            if feature['phase'] == '一期':
                assert refs, f"一期追踪缺项：{feature['id']}/{field}"
        if feature['phase'] != '一期':
            assert feature['target'] == '保留原期次，不前移', f"后续期次被改写：{feature['id']}"

    graph = {t['id']: identifier_refs(t['deps'], 'TASK-') for t in registries['TASK-']}
    # 总验收依赖既有一期任务；不将页面公共接线扩成反向依赖。
    graph['TASK-21'] = {f'TASK-{n:02d}' for n in range(21)}
    done, visiting = set(), set()
    def visit(task: str) -> None:
        assert task not in visiting, f'任务依赖成环：{task}'
        if task in done:
            return
        visiting.add(task)
        for dep in graph[task]:
            visit(dep)
        visiting.remove(task)
        done.add(task)
    for task in graph:
        visit(task)
    for case in registries['UAT-R']:
        assert case['status'] in ('未执行', '通过', '失败', '受阻'), f"未知验收状态：{case['id']}"
        if case['status'] != '未执行':
            assert case.get('evidence'), f"验收状态变化缺证据：{case['id']}"

def check_sources() -> None:
    """原件按原SHA验证；中文阅读副本只允许调整已有相对链接。"""
    entries = load('资料来源/来源清单.json')
    assert len({e['source_id'] for e in entries}) == len(entries)
    assert {f'S{n}' for n in range(1, 7)} <= {e['source_id'] for e in entries}
    source_dir = BUSINESS / '资料来源'
    for entry in entries:
        path = source_dir / entry['filename']
        assert path.is_file(), f'缺少来源文件：{path.name}'
        if 'archive' in entry:
            with ZipFile(source_dir / entry['archive']) as archive:
                raw = archive.read(entry['original_filename'])
            reading = raw.decode('utf-8')
            for other in entries:
                old_name = other.get('original_filename', other['filename'])
                reading = reading.replace(f"]({old_name})", f"]({other['filename']})")
            assert path.read_bytes() == reading.encode('utf-8'), f'历史副本有链接以外的修改：{path.name}'
        else:
            raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry['sha256'], f"原件SHA不一致：{entry['source_id']}"

def markdown_body(path: Path) -> str:
    """跳过围栏代码，避免把模板和旧路径示例当成可点击链接。"""
    lines = path.read_text(encoding='utf-8').splitlines()
    visible, block = [], []
    marker, language = '', ''
    for line in lines:
        fence = re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$', line)
        if not marker and fence:
            marker, language = fence.group(1), fence.group(2).strip().lower()
            block = []
        elif marker:
            if fence and fence.group(1)[0] == marker[0] and len(fence.group(1)) >= len(marker) and not fence.group(2).strip():
                if language == 'json':
                    json.loads('\n'.join(block))
                marker = ''
            else:
                block.append(line)
        else:
            visible.append(line)
    assert not marker, f'代码围栏未闭合：{path.relative_to(ROOT)}'
    return re.sub(r'`+[^`\n]*`+', '', '\n'.join(visible))

def markdown_links(body: str):
    """解析本仓使用的行内链接，允许路径带括号和百分号编码。"""
    for match in re.finditer(r'!?\[[^\]\n]+\]\(', body):
        start, depth, index = match.end(), 1, match.end()
        while index < len(body) and depth:
            char = body[index]
            if char == '\\':
                index += 2
                continue
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            index += 1
        assert depth == 0, f'链接括号未闭合：{match.group()}'
        destination = body[start:index - 1].strip()
        if destination.startswith('<'):
            destination = destination[1:destination.index('>')]
        else:
            destination = destination.split(' "', 1)[0]
        yield destination

def check_navigation(docs: list[Path], registries: dict) -> int:
    bodies = {p: markdown_body(p) for p in docs}
    anchors = {}
    for path, body in bodies.items():
        values = re.findall(r'<a\s+id="([^"]+)"\s*>', body)
        assert len(values) == len(set(values)), f'显式锚点重复：{path.relative_to(ROOT)}'
        anchors[path] = set(values)
    count = 0
    for path, body in bodies.items():
        for url in markdown_links(body):
            parts = urlsplit(url)
            if parts.scheme or parts.netloc:
                continue
            target = (path.parent / unquote(parts.path)).resolve() if parts.path else path
            assert target.is_relative_to(ROOT), f'链接越出仓库：{path.relative_to(ROOT)} → {url}'
            assert target.is_file(), f'链接目标不存在：{path.relative_to(ROOT)} → {url}'
            if parts.fragment:
                assert unquote(parts.fragment) in anchors.get(target, set()), f'锚点不存在：{path.relative_to(ROOT)} → {url}'
            count += 1
    module_docs = [p for p in BUSINESS.glob('*/README.md') if p.parent.name not in ('共用规范', '结构化数据', '资料来源')]
    assert module_docs, '缺少业务模块导航'
    for path in module_docs:
        for filename in ('产品需求.md', '业务规则.md', '领域与技术设计.md', '实施计划.md', '验收用例.md', '决策记录.md'):
            assert any(urlsplit(url).path.endswith('/' + filename) for url in markdown_links(bodies[path])), f'模块缺少阅读环节：{path.parent.name}/{filename}'
    navigation = '\n'.join(bodies[p] for p in module_docs + [BUSINESS / '后续范围.md', BUSINESS / 'README.md'])
    for prefix in ('FR-R', 'BR-R', 'TASK-', 'UAT-R', 'PD-'):
        missing = {e['id'] for e in registries[prefix]} - identifier_refs(navigation, prefix)
        assert not missing, f'业务导航遗漏编号：{missing}'
    return count

def main() -> None:
    if not __debug__:
        raise ValueError('请勿使用python -O；优化模式会跳过assert检查')
    files = [p for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.relative_to(ROOT).parts]
    for path in files:
        for part in path.relative_to(ROOT).parts:
            if part in ('README.md', 'AGENTS.md', '.gitattributes'):
                continue
            stem = part.rsplit('.', 1)[0]
            assert re.search(r'[\u4e00-\u9fff]', stem) and not re.search(r'[A-Za-z]', stem), f'目录或文件未使用中文名称：{path.relative_to(ROOT)}'
        if path.suffix == '.json':
            json.loads(path.read_text(encoding='utf-8'))
    docs = sorted(p.resolve() for p in files if p.suffix == '.md')
    for name in ('README.md', '产品需求.md', '业务规则.md', '领域与技术设计.md', '实施计划.md', '验收用例.md', '决策记录.md'):
        assert (SPECS / name).is_file(), f'缺少主文档：{name}'
    features = load('结构化数据/范围追踪.json')
    assert len(features) == 123
    assert Counter(x['phase'] for x in features) == {'一期': 76, '二期': 38, '三期': 9}
    assert len({x['id'] for x in features}) == 123
    check_sources()
    registries = {
        'FR-R': load('结构化数据/需求索引.json'),
        'BR-R': load('结构化数据/规则索引.json'),
        'TASK-': load('结构化数据/任务索引.json'),
        'UAT-R': load('结构化数据/验收索引.json'),
        'PD-': load('结构化数据/决策索引.json'),
    }
    content = '\n'.join(p.read_text(encoding='utf-8') for p in docs if '资料来源' not in p.parts)
    for prefix, entries in registries.items():
        ids = {e['id'] for e in entries}
        assert len(ids) == len(entries), f'{prefix}定义重复'
        refs = identifier_refs(content, prefix)
        assert not refs - ids, f'{prefix}存在未定义引用：{refs-ids}'
    check_relations(registries, features)
    link_count = check_navigation(docs, registries)
    fixture = load('结构化数据/租洗算例.json')
    assets = fixture['assets']
    assert len(assets) == len({a['epc'] for a in assets}) == 200
    assert sum(a['locationId'] == '7001' for a in assets) == 120
    q = fixture['quota']
    assert q['Q'] + q['T'] - q['R'] - q['O'] == 50
    assert Decimal('50') * Decimal('2.00') - Decimal('4.00') == Decimal('96.00')
    assert Decimal('80.00') * Decimal('25') / 100 == Decimal('20.00')
    assert sum(Decimal(v) for v in ('2.00', '10.00', '1.00')) == Decimal('13.00')
    revision_count = check_revision_scenarios(fixture, {c['id'] for c in registries['UAT-R']})
    check_text_sync(registries)
    print(f'通过：{len(docs)}份Markdown、{link_count}处本地链接/锚点、中文路径及模块六类导航。')
    print('通过：来源原件SHA及阅读副本、123组原范围、编号关系、任务依赖及合成数据。')
    print(f'通过：R1.1的{revision_count}组数值算例及FR/BR/TASK/UAT/PD主版与JSON一致性。')
    print('验收登记状态：' + str(dict(Counter(c['status'] for c in registries['UAT-R']))))
    print('未执行：Java / MySQL / API / 四端 / 真机 / 支付 / 生产验证。')

if __name__ == '__main__':
    try:
        main()
    except (AssertionError, OSError, ValueError, KeyError, BadZipFile) as exc:
        print(f'检查失败：{exc}', file=sys.stderr)
        sys.exit(1)
