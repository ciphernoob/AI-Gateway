"""Regenerate the checked-in Grafana dashboard."""
import json
from pathlib import Path

panels = [
    ('Requests', 'sum(rate(gateway_requests_total[5m]))'),
    ('Errors', 'sum(rate(gateway_requests_total{status="failed"}[5m]))'),
    ('Fallback', 'sum(rate(gateway_fallback_total[5m]))'),
    ('Unknown usage', 'sum(gateway_unknown_usage_total)'),
    ('Request p95', 'histogram_quantile(0.95,sum(rate(gateway_request_seconds_bucket[5m])) by (le))'),
    ('TTFT p95', 'histogram_quantile(0.95,sum(rate(gateway_ttft_seconds_bucket[5m])) by (le))'),
    ('Tokens', 'sum(rate(gateway_tokens_total[5m])) by (direction)'),
    ('Unsettled attempts', 'gateway_unsettled_attempts'),
    ('Audit queue bytes', 'sum(gateway_audit_queue_bytes)'),
    ('Settlement failures', 'sum(gateway_settlement_failures_total)'),
    ('Audit failures', 'sum(gateway_audit_failures_total) by (stage)'),
    ('Incomplete audits', 'gateway_audit_incomplete_requests'),
]
dashboard = dict(uid='ai-gateway', title='AI Gateway', schemaVersion=39, version=1, refresh='5s',
                 time={'from': 'now-1h', 'to': 'now'}, panels=[
    dict(id=i + 1, title=title, type='timeseries', datasource=dict(type='prometheus', uid='prometheus'),
         gridPos=dict(x=(i % 3) * 8, y=(i // 3) * 7, w=8, h=7), targets=[dict(expr=expr, refId='A')])
    for i, (title, expr) in enumerate(panels)])
target = Path(__file__).resolve().parents[1] / 'monitoring/grafana/dashboards/gateway.json'
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(dashboard, indent=2) + '\n', encoding='utf-8')
