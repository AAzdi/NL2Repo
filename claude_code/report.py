"""Write a full-task benchmark report, including failed and unevaluated tasks.

Backfill an existing experiment with: python -m claude_code.report EXPERIMENT_DIR
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import io
import json
import math
from pathlib import Path
import re

from claude_code.trajectory import atomic_json, now


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _historical_counts(directory):
    counts = {}
    path = directory / 'benchmark.log'
    if path.exists():
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                match = re.search(r'Project (\S+) has (\d+) test cases \(from file:', line)
                if match:
                    counts[match[1]] = int(match[2])
    return counts


def _load_tasks(directory, config, issues):
    records = {}
    # Final published results override the matching state, never retry archives.
    for pattern in ('workspaces/*/task_state.json', 'result/*.json'):
        for path in sorted(directory.glob(pattern)):
            try:
                result = _read(path)
                task_id = result['task_uuid']
                if not isinstance(task_id, str) or not task_id:
                    raise ValueError('missing task UUID')
                records[task_id] = (result, str(path.relative_to(directory)))
            except (OSError, ValueError, KeyError, TypeError):
                issues.append(f'无法读取任务记录：{path.relative_to(directory)}')

    plan_path = directory / 'task-plan.json'
    if plan_path.exists():
        planned = _read(plan_path)['tasks']
        ids = [task['task_uuid'] for task in planned]
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate task UUIDs in task-plan.json')
        selected = [(task, *records.pop(task['task_uuid'], ({}, None))) for task in planned]
        source = 'task-plan.json'
    else:
        if config is None and (directory / 'config.json').exists():
            config = _read(directory / 'config.json')
        if config and config.get('startPro'):
            counts = _historical_counts(directory)
            available = sorted(set(counts) | {r.get('pro_name') for r, _ in records.values()
                                             if r.get('pro_name')})
            buckets = defaultdict(list)
            for task_id, (result, path) in records.items():
                buckets[(result.get('module_name'), result.get('pro_name'))].append(task_id)
            selected = []
            for pro in config['startPro']:
                names = pro['proNameList']
                if names == ['*']:
                    if not counts:
                        raise ValueError('Cannot recover wildcard task scope without task-plan.json '
                                         'or historical benchmark counts')
                    names = available
                for name in dict.fromkeys(names):
                    key = (pro['moduleName'], name)
                    task_id = buckets[key].pop(0) if buckets[key] else None
                    task = dict(task_uuid=task_id, module_name=key[0], pro_name=name,
                                official_test_count=counts.get(name))
                    selected.append((task, *records.pop(task_id, ({}, None))))
            source = 'config.json'
        else:
            selected = [(result, result, path) for result, path in records.values()]
            records = {}
            source = 'task records'
            issues.append('缺少任务计划和配置，只能统计已发现的任务，无法确认全量任务范围。')
    if records:
        issues.append(f'忽略不属于本次任务计划的 {len(records)} 份任务记录。')
    if not selected:
        raise ValueError('No configured tasks or task records found')
    return selected, source


def _row(task, result, path, issues):
    pytest = result.get('post_process_result', {}).get('pytest_results', {})
    passed = result.get('score')
    if passed is None:
        passed = result.get('test_score', pytest.get('passed', 0))
    if not _number(passed):
        issues.append(f"{task.get('pro_name')}: 非法通过数，按无可用分数处理。")
        passed = 0
    total, denominator_source = None, None
    for value, source in ((task.get('official_test_count'), 'task plan / historical log'),
                          (result.get('official_test_count'), 'task state'),
                          (pytest.get('total'), 'pytest_results.total')):
        if _number(value) and value > 0:
            total, denominator_source = value, source
            break
    score_valid = passed > 0 or result.get('score_valid') is True
    rate = 0.0
    if passed > 0:
        if total is not None:
            rate = min(passed / total, 1.0)
        elif _number(pytest.get('success_rate')) and 0 < pytest['success_rate'] <= 1:
            rate = pytest['success_rate']
            denominator_source = 'recorded success_rate (total unavailable)'
            issues.append(f"{task.get('pro_name')}: 缺少官方分母，使用已记录的通过率。")
        else:
            rate = None
            issues.append(f"{task.get('pro_name')}: 有正分但缺少官方分母，无法计算全量平均分。")
    return dict(task_uuid=task.get('task_uuid'), module_name=task.get('module_name'),
                pro_name=task.get('pro_name'), status=result.get('status', 'missing'),
                generation_status=result.get('generation_status'),
                evaluation_finished=bool(result.get('evaluation_finished_at')),
                evaluation_valid=result.get('evaluation_valid', False),
                original_score_valid=result.get('score_valid', False), score_valid=score_valid,
                passed=passed, official_total=total, denominator_source=denominator_source,
                score_rate=rate, failure_kind=result.get('failure_kind'),
                failure_stage=result.get('failure_stage'),
                candidate_failure_stages=','.join(sorted({x.get('stage', '')
                    for x in result.get('candidate_failures', [])})),
                evaluation_attempt_count=len(result.get('evaluation_attempts', [])),
                coverage_limited=result.get('coverage_limited', False),
                started_at=result.get('started_at'), finished_at=result.get('finished_at'),
                source_path=path)


def _metrics(rows):
    total_rate = (math.fsum(row['score_rate'] for row in rows)
                  if all(row['score_rate'] is not None for row in rows) else None)
    return dict(task_count=len(rows),
                full_task_average_score=total_rate / len(rows) if total_rate is not None else None,
                task_score_sum=total_rate, passed=sum(row['passed'] for row in rows),
                scored_task_count=sum(row['score_valid'] for row in rows),
                positive_task_count=sum(row['passed'] > 0 for row in rows),
                full_score_task_count=sum(row['score_rate'] == 1 for row in rows),
                evaluated_task_count=sum(row['evaluation_finished'] for row in rows),
                status_counts=dict(Counter(row['status'] for row in rows)),
                failure_counts=dict(Counter(row['failure_kind'] for row in rows if row['failure_kind'])))


def build_report(directory, *, config=None, run_state=None):
    directory = Path(directory).resolve()
    issues = []
    selected, scope_source = _load_tasks(directory, config, issues)
    rows = [_row(task, result, path, issues) for task, result, path in selected]
    rows.sort(key=lambda row: (row['module_name'] or '', row['pro_name'] or '', row['task_uuid'] or ''))
    if run_state is None:
        run_state = _read(directory / 'run-state.json') if (directory / 'run-state.json').exists() else {}
    # Copy only report metadata, not credentials, prompts or command-line errors.
    run = {k: run_state[k] for k in ('experiment_name', 'status', 'started_at', 'finished_at',
                                    'exit_code', 'task_count') if k in run_state}
    terminal = all(row['status'] not in ('queued', 'running', 'missing') for row in rows)
    if not run.get('status') or (run['status'] in ('running', 'starting') and terminal):
        run['status'] = ('interrupted' if any(row['status'] == 'interrupted' for row in rows)
                         else 'completed' if all(row['status'] == 'completed' and row['evaluation_valid']
                                                 for row in rows)
                         else 'failed' if terminal else 'incomplete')
    starts = [row['started_at'] for row in rows if row['started_at']]
    ends = [row['finished_at'] for row in rows if row['finished_at']]
    if starts:
        run.setdefault('started_at', min(starts))
    if ends and terminal:
        run.setdefault('finished_at', max(ends))
    if run.get('started_at') and run.get('finished_at'):
        run['duration_seconds'] = (datetime.fromisoformat(run['finished_at'])
                                   - datetime.fromisoformat(run['started_at'])).total_seconds()
    scope_complete = scope_source != 'task records'
    if run.get('task_count') is not None and run['task_count'] != len(rows):
        issues.append(f"运行状态的任务数 {run['task_count']} 与恢复的任务范围 {len(rows)} 不一致。")
        scope_complete = False
    models = sorted({row['module_name'] for row in rows}, key=lambda value: value or '')
    metrics = _metrics(rows)
    model_metrics = [dict(module_name=model, **_metrics([row for row in rows if row['module_name'] == model]))
                     for model in models]
    if not scope_complete:
        # A discovered subset must not be presented as the full-task average.
        metrics['full_task_average_score'] = None
        for model in model_metrics:
            model['full_task_average_score'] = None
    return dict(schema_version=1, experiment_name=run.get('experiment_name') or directory.name,
                generated_at=now(), run=run, task_scope_source=scope_source,
                task_scope_complete=scope_complete,
                scoring_policy='positive-score-v1',
                formula='sum(min(passed_i / official_total_i, 1) for all configured tasks) / N; '
                        'tasks without a usable score contribute 0; positive scores count despite failures',
                metrics=metrics, models=model_metrics, issues=issues, tasks=rows)


def _cell(value):
    return str(value if value is not None else '—').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


def _percent(value):
    return f'{value:.4%}' if value is not None else '无法计算'


def render_markdown(report):
    metrics = report['metrics']
    run = report['run']
    lines = [f"# {_cell(report['experiment_name'])} 评测报告", '',
             f"## 全量任务平均分：{_percent(metrics['full_task_average_score'])}", '',
             '每个配置任务等权；任务分数 = min(测试通过数 / 官方测试总数, 1)。',
             '有正分即计入，即使任务失败；无可用分数或未评测任务记 0，分母包含全部配置任务。',
             '原先有效的 0 分仍有效。主分数不使用有效任务子集平均或测试用例加权平均。', '',
             f"计算：任务分数之和 {_cell(metrics['task_score_sum'])} / 全量任务数 {metrics['task_count']}。", '',
             '## 运行概况', '',
             f"- 状态：{_cell(run.get('status'))}；退出码：{_cell(run.get('exit_code'))}。",
             f"- 开始时间：{_cell(run.get('started_at'))}；结束时间：{_cell(run.get('finished_at'))}（ISO 8601）。",
             f"- 耗时（秒）：{_cell(run.get('duration_seconds'))}。",
             f"- 全量任务：{metrics['task_count']}；已结束评测：{metrics['evaluated_task_count']}；"
             f"有效评分：{metrics['scored_task_count']}；正分：{metrics['positive_task_count']}；"
             f"满分：{metrics['full_score_task_count']}；累计通过数：{metrics['passed']}。",
             '- 任务状态：' + '；'.join(f'{_cell(k)}={v}' for k, v in metrics['status_counts'].items()) + '。',
             f"- 任务范围来源：{report['task_scope_source']}；生成时间：{report['generated_at']}。", '',
             '## 按模型统计', '', '| 模型 | 全量任务数 | 全量任务平均分 |', '|---|---:|---:|']
    for model in report['models']:
        lines.append(f"| {_cell(model['module_name'])} | {model['task_count']} | "
                     f"{_percent(model['full_task_average_score'])} |")
    lines += ['', '## 失败与未评测任务', '',
              '失败状态和 evaluation_valid 保留执行故障信息，不影响已记录正分的计入。', '',
              '| 模型 | 任务 | 状态 | 故障类型 / 阶段 | 候选代码失败阶段 | 计入分数 |',
              '|---|---|---|---|---|---:|']
    failures = [row for row in report['tasks'] if row['status'] != 'completed' or not row['score_valid']]
    for row in failures:
        lines.append('| ' + ' | '.join(map(_cell, (row['module_name'], row['pro_name'], row['status'],
                     f"{row['failure_kind'] or '—'} / {row['failure_stage'] or '—'}",
                     row['candidate_failure_stages'] or '—', _percent(row['score_rate'])))) + ' |')
    if not failures:
        lines += ['', '无。']
    if report['issues']:
        lines += ['', '## 数据完整性说明', ''] + [f'- {_cell(issue)}' for issue in report['issues']]
    lines += ['', '## 全部任务明细', '',
              '| 模型 | 任务 | 状态 | 通过数 / 官方总数 | 分数有效 | 计入分数 | 结果来源 |',
              '|---|---|---|---:|---|---:|---|']
    for row in report['tasks']:
        lines.append('| ' + ' | '.join(map(_cell, (row['module_name'], row['pro_name'], row['status'],
                     f"{row['passed']} / {_cell(row['official_total'])}", '是' if row['score_valid'] else '否',
                     _percent(row['score_rate']), row['source_path']))) + ' |')
    lines += ['', '报告按任务 UUID 合并最终结果与任务状态；重试归档不额外计分。',
              '历史原始记录不修改，original_score_valid 保留原标记，score_valid 使用当前正分有效规则。',
              '机器可读汇总见 report.json，逐任务表格见 report.csv。', '']
    return '\n'.join(lines)


def _atomic_text(path, text):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def write_report(directory, *, config=None, run_state=None):
    directory = Path(directory).resolve()
    report = build_report(directory, config=config, run_state=run_state)
    atomic_json(directory / 'report.json', report)
    _atomic_text(directory / 'report.md', render_markdown(report))
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(report['tasks'][0]))
    writer.writeheader()
    writer.writerows(report['tasks'])
    _atomic_text(directory / 'report.csv', '\ufeff' + stream.getvalue())
    return report


def report_message(directory, report):
    return (f"全量任务平均分：{_percent(report['metrics']['full_task_average_score'])} "
            f"（{report['metrics']['task_count']} 个任务）；报告：{Path(directory).resolve() / 'report.md'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment_dir', type=Path)
    args = parser.parse_args()
    report = write_report(args.experiment_dir)
    print(report_message(args.experiment_dir, report))


if __name__ == '__main__':
    main()
