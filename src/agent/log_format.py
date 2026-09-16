"""平台 stderr 一行 JSON：固定字段顺序，带中文 title 方便扫读。"""
import json
import sys
from pathlib import Path

_COMMIT_ID = None
_COMMIT_LOGGED = False


def git_commit_id():
    """当前仓库 HEAD。读 .git 文件，不调用 git 命令；找不到则 unknown。"""
    global _COMMIT_ID
    if _COMMIT_ID is None:
        _COMMIT_ID = _read_git_head() or "unknown"
    return _COMMIT_ID


def _repo_root():
    return Path(__file__).resolve().parents[2]


def _resolve_git_dir(git_path: Path):
    if git_path.is_dir():
        return git_path
    if not git_path.is_file():
        return None
    try:
        text = git_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.lower().startswith("gitdir:"):
        return None
    target = Path(text.split(":", 1)[1].strip())
    if not target.is_absolute():
        target = (git_path.parent / target).resolve()
    return target if target.is_dir() else None


def _packed_ref(git_dir: Path, ref: str):
    packed = git_dir / "packed-refs"
    try:
        lines = packed.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == ref:
            return parts[0]
    return None


def _read_git_head():
    git_dir = _resolve_git_dir(_repo_root() / ".git")
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        ref = head[4:].strip()
        ref_file = git_dir / ref
        try:
            if ref_file.is_file():
                sha = ref_file.read_text(encoding="utf-8").strip()
                return sha or None
        except OSError:
            return None
        return _packed_ref(git_dir, ref)
    return head or None


def log_commit_banner(round_no=None):
    """本次进程只打一行 commit，作为执行日志的第一行。"""
    global _COMMIT_LOGGED
    if _COMMIT_LOGGED:
        return None
    _COMMIT_LOGGED = True
    sha = git_commit_id()
    short = sha[:12] if sha != "unknown" else "unknown"
    return emit_stderr(
        "BUILD_INFO", "commit", round_no,
        title=f"【版本】{short}",
        commit=sha,
    )


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
