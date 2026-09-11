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

**明确不实现（见 docs/rules_verified.md 未核实项，风险较高，留待后续版本）**：
- 三类任务系统（推理类/长上下文类/自进化类）：`acceptTask`/`submitAnswer`/`summonTreasure` 均未接入，答案 schema 未核实——这是唯一被有意搁置的部分,其余商店/消耗品体系已在 V1 完成。
- 眩晕法宝、范围炸弹（3×3 范围攻击/控场机器人）、机器人召唤令（花钱让对方下一夜机器人更多，纯进攻性道具）。
- `prompt`（LLM）与 `executeCmd`（沙盒）调用。
- 角色阵亡复活后的状态衔接、换边重置。

以上限制意味着 V1 是一个能打满全程、会挖矿卖矿、会建塔修塔升级、夜晚会防守的机器人，但还不会打任务系统这块最大的积分来源，也不会用眩晕/炸弹主动清场。

## 最小目录与迁移

```text
main.py             全部运行代码：SDK入口、callback、日志、预留接口
run.sh              接口文档启动样例（bash run.sh port）的转发脚本，转发给 python main.py
requirements.txt    Python依赖
README.md           启动、迁移、测试说明
tests/              本地测试和人工请求样例（运行时不需要）
tools/              开发期诊断脚本，如日志比对（运行时不需要）
docs/               赛题文档和审计（运行时不需要）
results/            验证记录（运行时不需要）
logs/               启动时自动创建，请求与响应日志
state/              启动时自动创建，预留状态目录
```

迁移运行只需复制 `main.py`、`run.sh`、`requirements.txt` 到一个可写目录，安装依赖后执行 `python main.py 6666` 或 `bash run.sh 6666`（二者等价，`run.sh` 只是按接口文档样例做的转发，判题平台具体靠哪种方式拉起程序尚未核实，两者都保留以防万一）。需要复现测试时，再复制 `tests/`；无需复制日志、状态或缓存。后续策略只修改 `callback` 及其调用的实现。

直接采用 SDK 的全局 `app = Flask(__name__)` 和 `process_request()`，去掉应用工厂和独立接口文件。粘贴文本里的 `**name**`/`**main**` 应还原为 Python 的 `__name__`/`__main__`，反斜线转义下划线也应还原。SDK 的回显 callback 被保底响应替换；保留非法 JSON 400、异常恢复和不覆盖日志。监听 `0.0.0.0` 遵循接口文档要求，区别于 Flask 默认仅本机监听。

## 启动与测试（PowerShell）

在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
python main.py 6666
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

- `logs/`、`state/` 相对 main.py 所在目录定位，不依赖工作目录。
- 每份合法 JSON 对象以 UTF-8 格式保存为 `logs/request_000001.json` 等，重启后跳过已有文件。保存的是格式化后的对象，不是原始 HTTP 字节。
- 非法 JSON、数组、null、标量或不符合 JSON Content-Type 的请求返回 HTTP 400，且不记录为合法请求。
- 日志或 callback 异常返回 HTTP 500 并记录异常到 stderr，服务继续处理后续请求。
- `state/build_memory.json`：`MatchState` 的跨回合学习记忆（建造失败黑名单、待建造目标、维修/升级任务队列、上一次发送的指令）落盘于此，`callback` 每次请求前读（进程内只读一次）、处理后写；写入用临时文件+原子替换防止写坏；没有任何值得记忆的内容时（如纯连通性测试请求）不落盘，`state/` 保持空目录。
- `main.py` 中 `GameState` 为抽象接口；`MatchState(GameState)` 实现 `update()`，将请求 JSON 解析为结构化字段（`Pos`/`Zone`/`MapInfo`/`Role`/`PlayerTask`/`TeamOur`/`TeamEnemy`/`RobotRole`/`WorldNews`/`ShopItem`/`ErrorInfo`），每回合全量重建。`Strategy` 由 `V1Strategy` 实现，`ActionValidator` 由 `BasicActionValidator` 实现，均已接入 `callback`。`TaskSession` 仍是未实现抽象接口（任务系统留待后续版本）。
- 当前使用 Flask 默认 JSON 解析，不宣称实现额外的重复键、NaN、请求大小、响应总时限等严格校验。
- `roleCommandMap` 里只有 V1 主动决定行动的角色才会出现在 map 中，其余角色本回合不下发指令（待机）；判题器是否接受"角色不出现在 map 中"为待机尚未经真实对局验证。
- 已通过 `tests/fixtures/sample_match_state.json`（本地构造、非官方样本）人工联通验证 V1 会针对真实结构的请求生成非空 `roleCommandMap`；尚未接入官方判题器实测决策效果。

不继续开发 C++、WSL 或 CMake。下一步：接上官方判题器验证 V1 白天经济/夜晚战斗闭环，再据反馈校准建造区、roundNo 起始值等假设，随后规划任务系统（V2）。
