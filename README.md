# 《未来战争》Python V1

## 多人协作开发

团队成员从 [开发与 GitHub 协作准则](CONTRIBUTING.md) 开始：任务认领、分支命名、PR 审查、目录职责、测试及发布流程均以该文档为准。仓库提供 Issue/PR 模板及 Python CI；远程分支保护和成员权限需要管理员按文档配置。

保留用户确认的官方 SDK 约定：`POST /` 接收 JSON 对象，由 `callback(json_data)` 返回 Python dict。V1 已接入真实策略：解析请求为结构化状态（`MatchState`）→ `V1Strategy` 按白天/夜晚生成指令 → `BasicActionValidator` 本地校验后返回 `roleCommandMap`。

## V1 范围

**已实现**：
- 状态解析（P1）：接口文档 1.1–1.7 全部字段映射为结构化实体。
- 8 方向 A* 寻路，碰撞避让（含同回合己方多角色目标格互斥）。
- 白天经济循环：工人采矿→背包接近满时前往小贩贩卖→机会性建造（武器优先补满 3 座，其次消耗石头建围墙）。
- 建造选址：因精确可建造区坐标未核实，采用"基地周围环形扩展试探 + 失败黑名单学习"的经验性策略，而非猜测蓝/黄区范围；该黑名单与待建造目标落盘到 `state/build_memory.json`，服务重启可恢复，不用每次从头试探。
- 夜晚战斗：角色贴身操控射程内武器攻击，按 BOSS>大型>中型>小型优先级选目标，同优先级内优先集火低血量；火箭冷却中不发起攻击；加特林/火箭按武器等级填充目标位置数。
- 生存兜底：任意角色（工人/开拓者）血量低于该类型满血一半且背包有 Medicine 时优先自我治疗（`use`），优先级高于经济/建造/战斗；路过武器商店且背包没药、金币够、背包有空位时顺手买一瓶，不为此专门绕路。满血数值按任务书4.5.1/4.5.2 表格（工人220/开拓者200/建筑按等级1000-4500不等）。
- 围墙维修与武器/围墙/基地升级券：空闲角色（工人或开拓者，`buy`/`use`/`sell` 是全角色可用动作）机会性执行"买道具→走到目标建筑一格内→use"两段式任务，优先级为 围墙维修（血量<满血80%）> 基地升级 > 武器升级（位置稀缺只有3座）> 围墙升级（数量不限，优先级最低）；同一目标建筑不会被两个角色重复分配。该任务队列（`worker_item_jobs`）与建造记忆一起落盘到 `state/build_memory.json`。
- 本地指令合法性校验（`BasicActionValidator`），拦截缺字段等明显非法指令；贩卖时只统计矿石类物品，不会把背包里的升级券/维修包/药品当矿石卖掉。

- 自进化任务已接入：pioneer 昼夜优先前往己方可用任务点并发出 `acceptTask`，收到 `phaseTask` 后原地保持任务。收到任务后提取 Markdown 路径，经平台沙盒读取文档，调用平台 LLM 解题，按需继续沙盒交互，最终提交 `submitAnswer`。

**尚未实现**：

- 推理类与长上下文类任务。
- 纯进攻性道具：眩晕法宝、范围炸弹（3×3 范围攻击）、机器人召唤令（花钱让对方下一夜机器人更多）。这些对生存没有直接威胁，经济和防守两个主线功能完整后再规划。
- 自进化任务以外的 `prompt`（LLM）与 `executeCmd`（沙盒）调用。
- 角色阵亡复活后的状态衔接、换边重置。

自进化任务解题链路已通过本地协议测试；实际得分、平台 LLM 输出和沙盒路径仍需官方对局验证。

## 项目结构与迁移

```text
main3.py                 程序入口（简洁转发，实际逻辑在 src/agent/）
src/agent/
  ├── __init__.py      导出公开接口
  ├── protocol.py      数据结构和状态定义
  ├── grid.py          网格算法和寻路
  ├── brain.py         策略和决策逻辑
  └── server.py        HTTP服务器和持久化
requirements.txt        Python 依赖（Flask, etc.）
README.md              启动、迁移、测试说明
.github/               GitHub Workflow 和 PR 模板
docs/                  赛题文档和审计
tests/                 本地测试用例（运行时不需要）
logs/                  启动时自动创建，请求/响应日志
state/                 启动时自动创建，跨回合学习记忆
```

**运行时最小依赖**：只需 `main3.py`、`src/agent/`、`requirements.txt` 三个部分。迁移运行：
1. 复制 `main3.py`、`src/agent/` 目录、`requirements.txt` 到可写目录
2. 执行 `pip install -r requirements.txt`
3. 执行 `python main3.py 6666` 启动服务

`logs/` 和 `state/` 会在运行时自动创建。无需复制测试、文档、日志或状态。

**架构说明**：代码采用标准 Python 包结构，核心逻辑分解为四个专职模块：
- `protocol.py`：数据协议与状态定义
- `grid.py`：网格算法（A* 寻路、碰撞检测）
- `brain.py`：游戏策略与决策（V1Strategy）
- `server.py`：HTTP 服务器与持久化

后续优化只需修改 `src/agent/brain.py` 中的策略实现，无需改动 HTTP 层或数据解析。

## 启动与测试（PowerShell）

在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
python main3.py 6666
```

官方比赛环境为 Python 3.11.10（《编译运行环境说明》仅确认版本号，未说明依赖包版本或其他限制）。开发机当前已安装 Python 3.12.7、Flask 3.0.3；代码未使用 3.12 专属语法，与 3.11.10 兼容，但建议提交前在 3.11.10 环境下复测一次。端口已被占用时请使用其他端口，不要停止原有服务。

另开 PowerShell，使用文件发送 JSON，避免 Windows 命令行的引号转义差异：

```powershell
curl.exe -i --noproxy "*" -X POST http://127.0.0.1:6666/ -H "Content-Type: application/json" --data-binary "@tests/fixtures/valid_request.json"
```

预期 HTTP 200，JSON 内容为上述固定对象，键顺序不作要求。fixture 是人工联通输入，不是官方对局请求。

```powershell
python -m unittest discover -s tests -v
```

测试包含临时目录和独立子进程的真实 HTTP 200→400→200 检查，自动关闭自己启动的子进程，不操作已有服务。测试结果见 `results/baseline/python-tests.log`。

## 实际行为与边界

- `logs/`、`state/` 相对 main3.py 所在目录定位，不依赖工作目录。
- 每份合法 JSON 对象以 UTF-8 格式保存为 `logs/request_000001.json` 等，重启后跳过已有文件。保存的是格式化后的对象，不是原始 HTTP 字节。
- 非法 JSON、数组、null、标量或不符合 JSON Content-Type 的请求返回 HTTP 400，且不记录为合法请求。
- 日志或 callback 异常返回 HTTP 500 并记录异常到 stderr，服务继续处理后续请求。
- `state/build_memory.json`：`MatchState` 的跨回合学习记忆（建造失败黑名单、待建造目标、维修/升级任务队列、上一次发送的指令）落盘于此，`callback` 每次请求前读（进程内只读一次）、处理后写；写入用临时文件+原子替换防止写坏；没有任何值得记忆的内容时（如纯连通性测试请求）不落盘，`state/` 保持空目录。
- `src/agent/protocol.py` 中 `GameState` 为抽象接口；`MatchState(GameState)` 实现 `update()` 将请求 JSON 解析为结构化字段（`Pos`/`Zone`/`MapInfo`/`Role`/`PlayerTask`/`TeamOur`/`TeamEnemy`/`RobotRole`/`WorldNews`/`ShopItem`/`ErrorInfo`），每回合全量重建。`src/agent/brain.py` 中 `Strategy` 由 `V1Strategy` 实现，`ActionValidator` 由 `BasicActionValidator` 实现，均已接入 `src/agent/server.py` 的 `GameServer`。`TaskSession` 仍是未实现抽象接口（自进化任务由 PioneerTaskSolver 实现）。
- 当前使用 Flask 默认 JSON 解析，不宣称实现额外的重复键、NaN、请求大小、响应总时限等严格校验。
- `roleCommandMap` 里只有 V1 主动决定行动的角色才会出现在 map 中，其余角色本回合不下发指令（待机）；判题器是否接受"角色不出现在 map 中"为待机尚未经真实对局验证。
- 已通过 `tests/fixtures/sample_match_state.json`（本地构造、非官方样本）人工联通验证 V1 会针对真实结构的请求生成非空 `roleCommandMap`；尚未接入官方判题器实测决策效果。

不继续开发 C++、WSL 或 CMake。下一步：接上官方判题器验证 V1 白天经济/夜晚战斗闭环，再据反馈校准建造区、roundNo 起始值等假设，随后规划任务系统（V2）。

## 自进化任务与平台下载日志

上传 `main3.py`、`run.sh`、`requirements.txt` 和完整 `src/`，使用 `bash run.sh <port>` 启动。
pioneer 按距离选择 `teamOur.playerTasks` 中 `isValid=true` 且 `coldDownRounds=0` 的自进化任务点，在周围一格内发送无额外参数的 `acceptTask`。正在执行任务时保持位置，必要时原地使用生命药剂；任务结束后按系统最新任务点状态重新选择，不自行推算冷却。

解题由 `src/agent/task_solver.py` 的跨回合状态机实现：

1. 正则提取任务描述中的 `.md` 路径，支持反引号、引号、中文引号和普通路径；有空格的路径应在题目中用引号包围。
2. 通过 `executeCmd` 在平台沙盒运行 Python：优先读取指定路径；相对路径不存在时，先搜索当前目录，再在限时范围内搜索文件系统。同名文件有歧义时返回候选路径给 LLM，不任意选择。
3. 下一回合解析 `lastCmdResult`，校验请求关联标识。文档每页 6000 字符，继续分页直到读完；每份文档自动读取上限 60000 字符，超出部分提示 LLM 按需读取。
4. 将任务原文、已读取文档、工具结果构造成响应顶层 `prompt`，使用比赛接口的 LLM，无需额外 API Key。任务期间的 LLM 调用按接口文档 1.7 豁免每日额度；代码另设每任务最多 12 次调用，防止无限重试。
5. 下一回合解析 `llmResp`。要求 LLM 返回 `{"action":"execute","command":"..."}` 或 `{"action":"submit","taskAnswer":"..."}`。前者通过平台沙盒执行，再把真实输出交给 LLM；后者生成 pioneer 的 `submitAnswer`。如最终答案本身是 JSON，仍按比赛协议序列化为 `taskAnswer` 字符串。
6. 系统明确反馈答案错误或提交动作非法时，将反馈交给 LLM 修正。任务结束、队伍变化或回合倒退时清理会话；会话保存在 `state/task_session.json`，支持进程重启恢复。缺失工具执行反馈时交给 LLM判断，不自动重复执行未知副作用的命令。

任务日志标记为 `PIONEER_TASK`。读取文件和执行工具时，平台沙盒输出包含 `requestId`、`event`、正文或结果的 JSON，下一回合进入 `lastCmdResult`。未使用沙盒解题的任务回合补充 `printf` 诊断，包含 `solverStage`、角色反馈、任务原文分片。诊断不会覆盖读文件或工具命令。程序 stderr 同时记录任务、阶段、`llmResp` 和 `lastCmdResult`。

在平台运行结束后，从系统下载对局日志，搜索 `PIONEER_TASK`、`read_document`、`execute_tool`、`submitAnswer`、`prompt` 或 `llmResp`。发出接取或提交指令不代表成功，应结合系统下一回合的动作结果、任务原文与错误核对。此日志回传不依赖下载选手容器中的本地 `logs/` 目录。

现有 docs 未提供平台上传、下载 API，也未说明下载文件包含哪些字段。因此本地已验证协议链路，实际平台 LLM、沙盒环境和最终下载文件可见性仍需上传对局验证。搜索限时 7 秒、工具执行限时 10 秒，单次工具输出最多保留 6000 字符；错误和截断信息会反馈给 LLM。
