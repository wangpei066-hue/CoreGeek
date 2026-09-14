"""平台 stderr 一行 JSON：固定字段顺序，带中文 title 方便扫读。"""
import json
import sys


def clip(text, limit=4000):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...(+{len(text) - limit})"


def headline(text, limit=36):
    """title 用的短句：去掉换行，超长截断。"""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def pos_text(target_pos):
    if not target_pos:
        return ""
    first = target_pos[0] if isinstance(target_pos, list) else target_pos
    if isinstance(first, dict) and "x" in first and "y" in first:
        return f"@({first['x']},{first['y']})"
    return ""


ACTION_CN = {
    "build": "建造", "collect": "采集", "move": "移动", "attack": "开火",
    "buy": "购买", "use": "使用", "sell": "出售", "idle": "待机",
    "acceptTask": "接取任务", "submitAnswer": "提交答案",
    "summonTreasure": "召唤宝藏",
}


def command_text(cmd):
    if not cmd:
        return "待机"
    action = cmd.get("action") or "?"
    label = ACTION_CN.get(action, action)
    name = cmd.get("name") or ""
    extra = pos_text(cmd.get("targetPos"))
    if cmd.get("num"):
        extra = f"×{cmd['num']}" + extra
    if name:
        return f"{label} {name}{extra}".strip()
    return f"{label}{extra}".strip()


def emit_stderr(marker, event, round_no=None, title="", **fields):
    """marker → event → roundNo → title → 其余。字符串过长会截断。"""
    record = {"marker": marker, "event": event, "roundNo": round_no, "title": title or ""}
    for key, value in fields.items():
        if isinstance(value, str):
            record[key] = clip(value)
        else:
            record[key] = value
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), file=sys.stderr, flush=True)
    return record
