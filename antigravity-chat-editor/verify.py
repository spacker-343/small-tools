import json
from pathlib import Path

root = Path(__file__).resolve().parent / '.local'
path = root / 'last-forward.json'
if not path.exists():
    print('还没有请求转发记录；请在编辑后的会话发送一条新消息。')
else:
    result = json.loads(path.read_text(encoding='utf-8'))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get('state') == 'forwarded' and any(e.get('state') in ('rewritten', 'summary-rewritten') for e in result.get('evidence', [])):
        print('已在实际发往上游的请求中应用修改后的 AI 正文或重建摘要；上游 HTTP 状态：', result.get('status'))
    elif result.get('state') == 'blocked':
        print('请求被拦截，没有继续发送无法可靠替换的旧内容。')
    else:
        print('这条记录尚不能证明目标 AI 回复已替换；它可能是其他会话或账户状态请求。')
