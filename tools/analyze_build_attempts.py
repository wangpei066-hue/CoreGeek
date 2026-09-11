"""开发期诊断工具：离线核对 logs/ 里成对的 request_*.json / response_*.json，
核实我方发出的 build 指令是否真被判题器接受（建造选址是猜的，见 docs/rules_verified.md）。

不属于运行时代码，不需要随 main.py 一起迁移；直接在项目根目录运行：
    python tools/analyze_build_attempts.py
可选传入自定义日志目录：
    python tools/analyze_build_attempts.py path/to/logs
"""
import json
from pathlib import Path
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
BUILDING_TYPES = ("wall", "gatling", "railgun", "rocket")


def load_pairs(log_dir: Path):
    pairs = []
    for req_path in sorted(log_dir.glob("request_*.json")):
        seq = req_path.stem.split("_", 1)[1]
        resp_path = log_dir / f"response_{seq}.json"
        if not resp_path.exists():
            continue
        request_data = json.loads(req_path.read_text(encoding="utf-8"))
        response_data = json.loads(resp_path.read_text(encoding="utf-8"))
        pairs.append((seq, request_data, response_data))
    return pairs


def analyze(log_dir: Path):
    pairs = load_pairs(log_dir)
    if not pairs:
        print(f"{log_dir} 里没有成对的 request/response 日志。")
        print("先让服务实际接一场比赛/热身对局跑几轮（或用 curl 手动多发几轮请求），再回来跑这个脚本。")
        return

    seqs = [seq for seq, _, _ in pairs]
    by_seq = {seq: (req, resp) for seq, req, resp in pairs}

    total = confirmed = failed = warned = unknown = 0

    for i, seq in enumerate(seqs):
        req, resp = by_seq[seq]
        round_no = req.get("roundNo")
        role_command_map = resp.get("roleCommandMap", {})
        build_cmds = {rid: cmd for rid, cmd in role_command_map.items() if cmd.get("action") == "build"}
        if not build_cmds:
            continue

        next_seq = seqs[i + 1] if i + 1 < len(seqs) else None
        next_req = by_seq[next_seq][0] if next_seq else None
        results = next_req.get("lastRoundRoleActionResults", {}) if next_req else {}
        next_roles = next_req.get("teamOur", {}).get("roles", []) if next_req else []

        for role_id, cmd in build_cmds.items():
            total += 1
            target = (cmd.get("targetPos") or [{}])[0]
            name = cmd.get("name")

            if next_req is None:
                status = "UNKNOWN（这是日志里的最后一轮，还没有下一轮反馈）"
                unknown += 1
            else:
                success_flag = results.get(role_id)
                structurally_built = any(
                    r.get("pos") == target and r.get("roleType") in BUILDING_TYPES for r in next_roles
                )
                if success_flag is True and structurally_built:
                    status = "CONFIRMED 成功（判题器标记合法 + 下一轮确实出现该建筑）"
                    confirmed += 1
                elif success_flag is False:
                    status = "FAILED（判题器判定该指令非法，选址大概率不在可建造区）"
                    failed += 1
                elif success_flag is True and not structurally_built:
                    status = "WARN：标记合法但下一轮未见对应建筑，需人工核对建筑坐标/等级覆盖等情况"
                    warned += 1
                else:
                    status = "UNKNOWN（下一轮 lastRoundRoleActionResults 未包含该角色 ID）"
                    unknown += 1

            print(f"round={round_no} seq={seq} role={role_id} name={name} target={target} -> {status}")

    print()
    print(f"共 {total} 次 build 尝试：确认成功 {confirmed}，判定失败 {failed}，需人工核对 {warned}，无法判定 {unknown}")
    if failed:
        print("失败的坐标会被 main.py 的 MatchState.failed_build_spots 自动拉黑（仅在同一进程存活期间生效，"
              "重启服务后清空，重启后会重新试探）。")


if __name__ == "__main__":
    log_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOG_DIR
    analyze(log_dir)
