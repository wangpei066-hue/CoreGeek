# 本地任务实验环境

生成六个任务、工作区模板和原始轨迹：

```bash
python3 tools/extract_local_tasks.py \
  --log logs/task.log \
  --output /tmp/selfEvolutionTask/1-fixed-step
```

输出包含：

- `1-unknown-api/`：三个 API 任务、`API_DOCS.md` 和可替换的模拟数据集；
- `2-engineering-fix/`：三个部署任务和每次可重置的 `ws_N.template`；
- `replay/`：按任务拆分的原始 JSONL 回合轨迹；
- `manifest.json`：任务顺序、数据来源说明和部署 token。

任务正文和规范来自 `read_document.content`。日志没有包含 API 的完整三城记录，因此
`dataset.json` 是显式标记的本地模拟数据，不冒充线上原始数据。

`LocalTaskEnvironment.reset()` 为每轮实验建立新的临时工作区，并返回以“请阅读…获取任务信息”
开头的 `phaseTask`。`submit()` 负责确定性判题；部署任务由环境独立检查文件状态，不信任
LLM 可以修改的 `check` 内容。

`tools/run_local_task.py` 中的 `LocalTaskDriver` 使用现有 `GameServer` 的 Flask test client 推进
完整回合。调用方可以传入一个 `Callable[[str], str]` 作为 LLM：

```python
from pathlib import Path
from src.agent.local_task_env import LocalTaskEnvironment
from tools.run_local_task import LocalTaskDriver

env = LocalTaskEnvironment(
    Path("/tmp/selfEvolutionTask/1-fixed-step"),
    "task_1_alpha.md",
)
result = LocalTaskDriver(env, llm=lambda prompt: call_your_llm(prompt)).run()
```

也可以直接使用百炼的 OpenAI 兼容接口。密钥只通过环境变量读取：

```bash
export DASHSCOPE_API_KEY='重新生成的密钥'
python3 tools/run_local_task.py task_1_alpha.md \
  --model qwen3.6-27b \
  --trace /tmp/local-task-traces/task_1_alpha.json
```

默认关闭百炼的 thinking 模式，确有需要时可传入 `--thinking` 开启。流中的
`reasoning_content` 不会写入求解器反馈或实验轨迹，只有最终 `content` 会作为 `llmResp`。

批量运行六个任务并跨任务保留 solver experience：

```bash
PYTHONPATH=.local_deps:. python3 tools/run_local_suite.py \
  --output /tmp/local-task-report.json
```

API 类任务会自动在 `127.0.0.1:8899` 启动本地服务。服务刻意复现日志中的文档偏差：
实际认证为 `Authorization: Bearer`，实际城市参数为 `location`，分页参数为
`offset/limit`。

回合驱动遵守接口文档的 `executeCmd` 传输约定：15 秒超时、64KB 截断，以及
`[exitCode:N]`、`[TIMEOUT]`、`[TRUNCATED]` 标记。错误答案会返回合法动作结果和
`errorCode=2`；耗尽 `timeoutRounds` 后以 `errorCode=1` 结束。

当前模拟范围从平台已经给出非空 `phaseTask` 开始，尚不模拟任务点移动和 `acceptTask`。
开拓者离开任务点或死亡导致任务结束的游戏世界行为也不在本任务实验环境内。Docker 在当前
WSL 未安装，因此命令隔离仍为宿主机子进程；在启用 Docker Desktop WSL integration 前，
只能使用可信模型和专用临时工作区。

当前驱动器只在本机子进程执行 solver 生成的包装命令，尚未加入 Docker 隔离；不要把它用于
不可信模型或含宿主机敏感数据的环境。

运行测试需要先安装项目的开发依赖：

```bash
python3 -m pip install -e '.[dev]'
python3 -m unittest -q tests.test_local_task_env
```
