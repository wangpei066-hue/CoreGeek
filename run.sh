#!/usr/bin/env bash
# 接口文档启动样例：bash run.sh port（docs/接口文档.md 开头）。
# 判题平台是否真的靠这个脚本拉起程序尚未核实（见 docs/current_audit.md 缺口清单）；
# 这里只做参数转发，不改变 main.py 的行为，即使平台不用它也无副作用。
set -euo pipefail
cd "$(dirname "$0")"

if [ "$#" -ne 1 ]; then
    echo "usage: bash run.sh <port>" >&2
    exit 1
fi

# command -v 只能确认命令名存在，确认不了它是不是个空壳（例如 Windows 的
# python3 商店执行别名，未安装时存在但什么也不做）；这里额外跑一次空脚本验证真能执行。
pick_python() {
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "" >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON_BIN="$(pick_python)" || { echo "run.sh: 找不到可用的 python/python3 解释器" >&2; exit 1; }
exec "$PYTHON_BIN" main.py "$1"
