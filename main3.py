"""V1 HTTP服务。请求解析为结构化GameState，由V1Strategy生成指令。

跨回合学习记忆（建造黑名单等）在此处读盘/落盘，保持V1Strategy.decide()本身无IO。
"""
import argparse
import sys
from pathlib import Path

from src.agent import GameServer

ROOT = Path(__file__).resolve().parent


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", line_buffering=True)

    parser = argparse.ArgumentParser(description="P0 competition HTTP service")
    parser.add_argument("port", type=int)
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    server = GameServer(ROOT)
    server.prepare_directories()
    server.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
