# 当前审计：Python P0

日期：2026-09-10（Asia/Shanghai）。结论：Python P0 本地验收通过；官方对局验收未完成。

## 材料与范围

- 本次开始时工作区仅有 docs/；未发现 main.py 或 Python SDK 文件。按用户提供并确认为官方 SDK 的代码/约定创建 main.py。因此 SDK 来源是本次用户提供内容，未独立验证官方发布包版本。
- 已阅读本地《任务书》v1.0、《接口文档》v1.0、request.txt、response.txt；此前读取的 C++ 工程在切换时已不在当前工作区，不继续修改、构建或运行。
- 此前 C++ WSL 副本复制因元数据权限失败，未完成 C++ 基线；随后用户明确切换 Python。本轮结果不包含 C++ 成功编译结论。
- 无 Git 元数据，采用 results/baseline/source-sha256.json 标识文件版本。

## 模块状态及证据

- HTTP 入口【实现+运行验证】：main.py:app/process_request，POST /；其他路径 404，GET / 405。真实进程绑定 0.0.0.0，独立临时端口测试通过。
- 请求响应【实现+运行验证】：只要求请求为 JSON 对象，未验证赛题字段。callback 固定返回三个保底字段；测试确认调用唯一策略入口且没有回显请求。
- 日志【实现+运行验证】：合法对象 UTF-8 序列化、顺序编号、独占创建；重启保留旧日志测试通过。不保存非法请求原文，也没有响应日志、回合关联或比赛指标。
- 状态管理【仅目录+接口】：state/ 存在且为空；GameState 抽象 update，未映射字段、未实现持久化/换边/回合重置。
- 寻路【未实现】：没有路径搜索、占位处理、碰撞规避代码。
- 经济【未实现】：没有采集、出售、购买、建造循环。
- 战斗【未实现】：没有日夜判断、操控分配、攻击或归位逻辑。
- 任务编排【仅接口】：TaskSession 抽象 update；没有任务状态机、答案生成、LLM 提示词、沙盒命令或技能复用。
- Strategy【仅接口】：抽象 decide，不会生成实际游戏动作。
- ActionValidator【仅接口】：抽象 validate，未做规则合法性校验。HTTP JSON 对象判断不等于动作验证。
- 异常处理【实现+运行验证】：非法 JSON 对象返回 400；callback 故障注入返回 500，之后请求仍成功。日志写入故障处理有代码，未做磁盘故障运行测试。
- 测试【新增本地测试已运行】：tests/test_p0.py 共 8 项通过；人工 fixture，不是官方状态样本。没有可继续执行的既有 Python 测试或官方测试。

## 执行与验收

执行 `python -m unittest discover -s tests -v`，退出码 0，8/8 通过，运行约 3.1 秒。真实子进程 HTTP 返回 200→400→200，并保持存活；测试结束仅终止自建子进程。未保留后台测试进程，生产服务需按 README 手动启动。

results/baseline/python-tests.log 保存完整测试输出与真实 HTTP 服务日志；其中 injected test failure 是预期故障注入，不是测试失败。python-tests.exit.txt 保存退出码。commands.json 保存命令、环境、版本标识与验证范围。

本轮创建 Python 服务、未实现接口、测试、依赖锁定及说明；未修改官方文档和样例。没有开发游戏策略，没有将本地 fixture 对局化。

## 仍缺的材料

1. 判题器 POST / 的第一份真实请求 JSON（保留完整结构、字段类型、空值与实际响应反馈），以及后续昼夜边界、任务与换边请求，才能核验状态生命周期。
2. 官方判题器/最小对局启动器、运行命令、地图及对手配置。当前无法运行官方对局，也无法确认空动作待机被接受。
3. 官方 Python SDK 发布包/版本、依赖与提交打包要求。用户已确认 HTTP/SDK 入口；《编译运行环境说明》已由用户核对，仅确认 Python 3.11.10，依赖包版本等仍未说明，发布物和评测环境仍未独立验证。
4. 建造区坐标、回合从 0/1 开始、换边重启/状态清理约定、实际任务答案 schema 等缺口，详见 rules_verified.md。
5. 【2026-09-11 补充】已按接口文档开头"样例：bash run.sh port"新增 `run.sh`（转发给 `python main.py`），本地在 Windows + Git Bash 下语法检查通过且实测能正常拉起服务、响应 200；但判题平台是否真的用 `run.sh` 拉起程序、提交包目录结构要求，仍未从赛事组委会得到确认。

服务可启动、本地测试可追溯；官方对局不可运行（材料缺失）。停止于 Python P0，不推进 E1–E7。第一份真实 JSON 是接入状态的下一项关键输入，但并不能替代官方判题器和完整运行/提交材料。

## SDK 单文件架构整理（2026-09-10）

依据用户随后提供的 SDK 源码，保留全局 app、POST / 的 process_request、callback、jsonify 调用链。移除 create_app 工厂，将四个未实现抽象接口并入 main.py，删除 interfaces.py。SDK 原样回显仍替换为 P0 固定响应；未加入策略。README 记录最小两文件迁移和与 SDK 的有意差异。

重新执行 python -m unittest discover -s tests -v：8/8 通过，退出码0。真实进程测试只复制 main.py 到临时目录，从另一工作目录启动，验证200→400→200及日志落盘。结果见 results/baseline/python-sdk-layout-tests.log；早期 python-tests.log 和 source-sha256.json 保留为旧版历史证据，新版见 sdk-layout-manifest.json。测试未改变现有服务。

## P1：状态解析（2026-09-10）

新增 `MatchState(GameState)`，将请求体解析为结构化实体（`Pos`/`Zone`/`MapInfo`/`Role`/`PlayerTask`/`TeamOur`/`TeamEnemy`/`RobotRole`/`WorldNews`/`ShopItem`/`ErrorInfo`），`callback` 接入 `match_state.update(json_data)`，仍固定返回空动作。新增 `tests/test_state_parser.py`（用修正过 JSON 语法的 `tests/fixtures/sample_match_state.json`，非官方样本）。执行 `python -m unittest discover -s tests -v`：20/20 通过。

## V1：经济+战斗策略（2026-09-11）

用户确认官方 Python 版本 3.11.10（《编译运行环境说明》仅确认版本号）。在 P1 状态解析基础上实现：

- 寻路【实现+单测验证】：8 方向 A*（切比雪夫启发式），`build_blocked_set` 汇总己方/敌方建筑（含基地 2x2 占位）、角色、机器人为障碍；`move_towards` 处理"已在目标一格内"与"绕障碍寻路"两种情形。
- 经济【实现+单测验证】：工人按 采矿→背包超阈值贩卖→机会性建造 的优先级循环；建造选址用"基地周围环形试探+失败黑名单"经验策略，因精确可建造区坐标未核实（见 rules_verified.md）而不采用猜测坐标。
- 战斗【实现+单测验证】：夜晚角色贴身操控射程内武器，目标按 BOSS>大型>中型>小型优先级+同级最低血量优先选取；火箭冷却检查；加特林/火箭按等级填充目标位置数组长度。
- 校验【实现+单测验证】：`BasicActionValidator` 拦截缺字段等明显非法指令，不合法指令直接丢弃（该角色当回合待机）而非发送。
- 明确未实现：三类任务系统（`acceptTask`/`submitAnswer`/`summonTreasure`）、商店消耗品/升级券购买使用、`prompt`/`executeCmd` 调用、复活/换边状态衔接——均依赖当前仍缺失或未核实的材料（任务答案 schema、可建造区精确坐标等）。

新增 `tests/test_v1_strategy.py`（45 项全部通过，覆盖寻路、昼夜判定、目标优先级、日间/夜间决策、校验器）；`tests/test_state_parser.py` 中依赖旧版"固定空响应"的断言已更新为结构断言。人工用 `sample_match_state.json` 起真实 HTTP 服务验证：请求为夜晚回合（roundNo=85）时返回的 `roleCommandMap` 非空、结构合法。仍未接入官方判题器，V1 决策质量未经真实对局验证。

## V1 补全：响应日志、run.sh、跨回合持久化、生存兜底（2026-09-11）

- 响应日志：`process_request` 现在同时落盘 `logs/request_NNNNNN.json` 与 `logs/response_NNNNNN.json`（同序号配对），配合新增的 `tools/analyze_build_attempts.py` 离线核对 `build` 指令是否被判题器接受（用下一轮 `lastRoundRoleActionResults` + `teamOur.roles` 结构双重验证）。
- `run.sh`：按接口文档开头"样例：bash run.sh port"补充，转发给 `python`/`python3`（用实际执行一次空脚本而非仅 `command -v` 判断可用性，避开 Windows python3 商店空壳的坑）。本地 Git Bash 下语法检查、实际拉起服务、响应 200 均验证通过；判题平台是否真的依赖它仍未核实。
- 跨回合记忆持久化：`load_build_memory`/`save_build_memory` 把建造黑名单、待建造目标、上一次发送的指令落盘到 `state/build_memory.json`，`callback` 请求前读、处理后写；无内容可记时不落盘。`V1Strategy.decide()` 本身保持无 IO，磁盘读写只发生在 `callback()`，便于策略单测继续用内存态 `MatchState` 直接跑。
- 生存兜底：`max_health()` 按任务书4.5.1/4.5.2 表格给出各单位满血值；`decide_self_heal` 让血量低于满血一半且背包有 Medicine 的角色优先自愈（优先级高于经济/建造/战斗，白天夜晚均生效）；`decide_buy_medicine` 让角色路过武器商店时机会性补给。
- 商店升级/维修体系：`maybe_start_shop_item_job`/`decide_shop_item_job` 实现"买道具→走到目标建筑→use"两段式任务，覆盖围墙维修（WallFixer）与武器/围墙/基地升级券，优先级 围墙维修>基地升级>武器升级>围墙升级；工人和开拓者均可执行（`buy`/`use`/`sell` 是全角色动作）；任务队列与建造记忆一起落盘。同时修了一个潜在 bug：贩卖逻辑原先用 `Counter(worker.backpack)` 不加过滤，如果背包里恰好升级券/药品数量最多会被当矿石误卖，现在只统计 `ORE_TYPES` 内的物品。
- 新增 `tests/test_persistence.py`、`tests/test_response_logging.py`、`tests/test_shop_items.py`，加上既有测试扩充，共 87 项全部通过（`python -m unittest discover -s tests -v`）。人工起服务验证过完整流程（含 `sample_match_state.json`）不产生异常，`state/`、`logs/` 内容符合预期后已清理测试产物。
- 仍未实现：三类任务系统（唯一有意搁置的部分，答案 schema 未核实）、眩晕法宝/范围炸弹/机器人召唤令、`prompt`/`executeCmd`。均已在 README 标注。用户计划接下来上传到真实判题环境测试，测试后会提供日志用于进一步分析校准。

## 自进化任务接取及平台日志回传（2026-09-11）

- 已实现：pioneer 昼夜选择可用自进化任务点、移动至一格范围、发送 `acceptTask`；以系统 `phaseTask` 判断任务执行期间并原地保持，可原地用药。任务结束后重新读取系统冷却和有效状态。
- 日志：stderr 输出 `PIONEER_TASK` 诊断；任务期间经 `executeCmd` 在判题沙盒输出任务原文分片和角色反馈，由协议规定的下一回合 `lastCmdResult` 回传，不依赖下载本地 logs 文件夹。发出接取请求与实际收到任务原文分别记录。
- 启动：run.sh 改为仓库实际存在的 main3.py；真实 HTTP 测试改为复制上传所需文件到临时目录后通过 run.sh 启动。
- 验证：93 项 unittest 全部通过，包含昼夜接取、移动、冷却/失效过滤、死亡过滤、重启后凭 phaseTask 保持、沙盒字符串引用与日志回传链路，以及真实 HTTP 启动。依赖安装在 /tmp/pioneer-test-deps，使用 PYTHONPATH 指定；真实 HTTP 测试需要沙盒外的本地端口权限。
- 未验证：尚未上传到官方平台、未下载官方日志；docs 未规定下载日志字段，不能把本地链路测试表述为系统下载验证。操作与搜索字段见 README 的“自进化任务与平台下载日志”。
- 范围：仅接取与观测，不自动求解或 submitAnswer；当前任务会等待平台超时。后续解题需要统一调度 executeCmd，避免与诊断输出竞争。

## 自进化任务自动解题（2026-09-11，接取版本的后续实现）

- 新增 task_solver.py：正则提取任务中的 Markdown 路径，经平台 executeCmd 查找文件、分页读取，通过下一回合 lastCmdResult 关联请求并收集正文，然后构造平台 prompt。
- 解析下一回合 llmResp 的结构化 JSON。LLM 可请求沙盒命令以调用任务 API/运行 Python，获得真实结果后继续解题；最终将字符串答案写入 pioneer 的 submitAnswer.taskAnswer。非法 LLM 输出或明确的答案错误反馈会触发重新求解。
- 自进化任务会话原子保存到 state/task_session.json，支持重启和相同回合重试；任务消失、队伍变化或回合倒退重置。协议没有任务唯一ID，连续同文任务仍依赖任务结束的空 phaseTask 快照区分。
- 系统日志：读取和工具执行输出 PIONEER_TASK JSON，含 requestId 和结果；空闲沙盒回合补充阶段诊断，避免诊断覆盖解题命令。stderr 同时记录 llmResp。平台下载可见性仍未实测。
- 限制：文件搜索7秒，工具执行10秒；文档每页6000字符、每文档自动读取最多60000字符；工具结果最多6000字符；每任务最多12次LLM调用。超限/找不到/歧义/截断信息反馈给LLM。未知工具反馈不自动重放命令。
- 验证：完整105项 unittest通过（含真实HTTP启动）；新增12项测试覆盖读文档→平台LLM→执行工具→平台LLM→提交、分页、路径查找与歧义、安全引用、错误答案、重启、重复回合及旧任务结果隔离。测试使用模拟LLM回复和实际本机执行生成的沙盒命令，未调用官方LLM或上传平台。

## worker 与先锋任务合并（2026-09-11）

- 合入 worker 4394cc3 的首日建造、围墙规划、独立武器分配、寻路、预算、记忆重置及决策日志。
- 保留 xql 的先锋任务接取、跨回合求解、沙盒诊断与会话恢复。开局和夜间先处理先锋任务，接管中的先锋不参与武器分配。
- 新增4项集成回归，验证工人建造与先锋接取并行、临近夜晚保持任务、任务中用药，以及先锋不会占用工人的武器分配。
- 使用临时依赖目录执行完整 unittest：134项全部通过，包含真实HTTP启动。尚未运行官方对局。
