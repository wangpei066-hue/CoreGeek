#!/usr/bin/env python3
"""Extract the six self-evolution tasks from logs/task.log.

The extractor treats read_document.content as the source of truth.  It also
writes replay trajectories and creates resettable deployment workspaces plus a
small, explicitly synthetic API dataset for local integration tests.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil


TASK_ORDER = (
    "task_1_alpha.md", "task_1_beijing.md", "task_2_nanjing.md",
    "task_2_beta.md", "task_3_chengdu.md", "task_3_gamma.md",
)
TOKENS = {"alpha": "fc1e78eb2a5a", "beta": "0de1b57493cf", "gamma": "c8be2288b213"}
SYNTHETIC_DATA = {
    "北京": [
        {"id": "BJ001", "name": "故宫", "type": "建筑", "era": "明清", "era_order": 1368,
         "protected_level": "世界遗产"},
        {"id": "BJ002", "name": "周口店遗址", "type": "遗址", "era": "旧石器时代", "era_order": -500000,
         "protected_level": "世界遗产"},
        {"id": "BJ003", "name": "颐和园", "type": "园林", "era": "清", "era_order": 1750,
         "protected_level": "世界遗产"},
    ],
    "南京": [
        {"id": "NJ001", "name": "明孝陵", "type": "陵墓", "era": "明", "era_order": 1381,
         "protected_level": "世界遗产"},
        {"id": "NJ002", "name": "南京城墙", "type": "建筑", "era": "明", "era_order": 1366,
         "protected_level": "全国重点"},
        {"id": "NJ003", "name": "六朝建康城遗址", "type": "遗址", "era": "六朝", "era_order": 229,
         "protected_level": "全国重点"},
    ],
    "成都": [
        {"id": "CD001", "name": "金沙遗址", "type": "遗址", "era": "商周", "era_order": -1200,
         "protected_level": "全国重点"},
        {"id": "CD002", "name": "青城山", "type": "宗教建筑", "era": "东汉", "era_order": 143,
         "protected_level": "世界遗产"},
        {"id": "CD003", "name": "武侯祠", "type": "建筑", "era": "蜀汉", "era_order": 223,
         "protected_level": "全国重点"},
    ],
}

# Make pagination unavoidable while retaining the log-derived Beijing examples.
for city, prefix, target in (("北京", "BJ", 15), ("南京", "NJ", 12), ("成都", "CD", 11)):
    records = SYNTHETIC_DATA[city]
    while len(records) < target:
        number = len(records) + 1
        records.append({
            "id": f"{prefix}{number:03d}", "name": f"{city}模拟遗产{number}",
            "type": ("建筑", "遗址", "园林")[number % 3], "era": "清",
            "era_order": 1700 + number, "protected_level": "全国重点",
        })


def load_rows(log_path: Path) -> list[dict]:
    rows = []
    raw = log_path.read_text(encoding="utf-8", errors="replace")
    # Some exports contain literal newlines inside a record. Split on the
    # stable outer-record marker instead of physical lines.
    records = re.split(r'(?=\{"roundNo")', raw)
    for line in records:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            # Older exported logs embedded the PIONEER_TASK JSON directly in
            # lastCmdResult without escaping its quotes. Recover the document
            # events from those lines so extraction remains possible.
            match = re.search(
                r'"event":"read_document".*?"path"\s*:\s*"([^"]+)".*?'
                r'"content":"(.*?)","nextOffset"',
                line,
            )
            if not match:
                continue
            try:
                content = json.loads('"' + match.group(2) + '"')
            except json.JSONDecodeError:
                continue
            value = {
                "lastCmdResult": json.dumps({
                    "marker": "PIONEER_TASK",
                    "event": "read_document",
                    "path": match.group(1),
                    "content": content,
                }, ensure_ascii=False),
            }
        if isinstance(value, dict):
            rows.append(value)
    return rows


def sandbox_payload(raw: str) -> dict | None:
    for line in (raw or "").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("marker") == "PIONEER_TASK":
            return value
    return None


def extract_documents(rows: list[dict]) -> dict[str, str]:
    documents = {}
    for row in rows:
        payload = sandbox_payload(row.get("lastCmdResult", ""))
        if payload and payload.get("event") == "read_document" and isinstance(payload.get("content"), str):
            documents[Path(payload.get("path", "unknown.md")).name] = payload["content"]
    missing = [name for name in TASK_ORDER if name not in documents]
    if missing:
        raise RuntimeError("task.log missing read_document content for: " + ", ".join(missing))
    return documents


def write_workspace(root: Path, app: str, number: int, spec: str) -> None:
    template = root / "2-engineering-fix" / f"ws_{number}.template"
    (template / "config").mkdir(parents=True, exist_ok=True)
    (template / "bin").mkdir(parents=True, exist_ok=True)
    (template / "spec.md").write_text(spec, encoding="utf-8")
    # Six lines, deliberately wrong at the two lines named by spec.md.
    (template / "config" / f"{app}.conf").write_text(
        "# generated fixture\nmode production\nport 1\nworkers 2\nregion local\nname broken\n", encoding="utf-8")
    start = template / "bin" / "start.sh"
    start.write_text("#!/bin/sh\necho started\n", encoding="utf-8")
    start.chmod(0o644)
    token = TOKENS[app]
    port = re.search(r"第 3 行：`?([^`\n]+)`?", spec).group(1).strip()
    service = re.search(r"第 6 行：`?([^`\n]+)`?", spec).group(1).strip()
    check = template / "check"
    check.write_text(
        "#!/bin/sh\nset -eu\nfail=0\n"
        f"[ -d logs/{app} ] || fail=$((fail+1))\n"
        f"mode=$(stat -c %a logs/{app} 2>/dev/null || stat -f %Lp logs/{app} 2>/dev/null || true)\n"
        "[ \"$mode\" = 755 ] || fail=$((fail+1))\n"
        f"[ \"$(sed -n '3p' config/{app}.conf 2>/dev/null)\" = {json.dumps(port)} ] || fail=$((fail+1))\n"
        f"[ \"$(sed -n '6p' config/{app}.conf 2>/dev/null)\" = {json.dumps(service)} ] || fail=$((fail+1))\n"
        "[ -f bin/start.sh ] || fail=$((fail+1))\n"
        "[ -x bin/start.sh ] || fail=$((fail+1))\n"
        "if [ \"$fail\" -eq 0 ]; then\n"
        f"  echo '[ OK ] 全部通过 (6/6)'; echo 'TOKEN: {token}'; exit 0\n"
        "fi\n"
        "echo \"[FAIL] $fail 项未通过\"; exit 1\n",
        encoding="utf-8", newline="\r\n")
    check.chmod(0o755)


def extract(log_path: Path, output: Path) -> dict:
    rows = load_rows(log_path)
    docs = extract_documents(rows)
    specs = {}
    for row in rows:
        payload = sandbox_payload(row.get("lastCmdResult", ""))
        if not payload or payload.get("event") != "read_document" or Path(payload.get("path", "")).name != "spec.md":
            continue
        match = re.search(r"/ws_(\d+)/spec\.md$", payload.get("path", ""))
        if match:
            specs[int(match.group(1))] = payload["content"]
    if set(specs) != {1, 2, 3}:
        raise RuntimeError("task.log does not contain all three workspace specs")
    if output.exists():
        shutil.rmtree(output)
    (output / "1-unknown-api").mkdir(parents=True)
    (output / "2-engineering-fix").mkdir(parents=True)

    for name in TASK_ORDER:
        group = "1-unknown-api" if any(x in name for x in ("beijing", "nanjing", "chengdu")) else "2-engineering-fix"
        (output / group / name).write_text(docs[name], encoding="utf-8")
    (output / "1-unknown-api" / "API_DOCS.md").write_text(docs["API_DOCS.md"], encoding="utf-8")
    (output / "1-unknown-api" / "dataset.json").write_text(
        json.dumps(SYNTHETIC_DATA, ensure_ascii=False, indent=2), encoding="utf-8")

    for number, app in enumerate(("alpha", "beta", "gamma"), 1):
        write_workspace(output, app, number, specs[number])

    trajectories = {name: [] for name in TASK_ORDER}
    active = None
    for row in rows:
        phase = row.get("phaseTask", "")
        named = next((name for name in TASK_ORDER if name in phase), None)
        if named:
            active = named
        if active:
            trajectories[active].append(row)
    replay_dir = output / "replay"
    replay_dir.mkdir()
    for name, items in trajectories.items():
        (replay_dir / name.replace(".md", ".jsonl")).write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items), encoding="utf-8")

    manifest = {
        "source": str(log_path.resolve()), "task_order": list(TASK_ORDER),
        "api_dataset": "synthetic-replaceable; full original records were not present in read_document.content",
        "deployment_tokens": TOKENS,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, default=Path("logs/task.log"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/selfEvolutionTask/1-fixed-step"))
    args = parser.parse_args()
    print(json.dumps(extract(args.log, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
