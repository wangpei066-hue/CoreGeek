# 三题一组的自进化模拟题

新版依据 `docs/任务书.md` §5.3 和原始 `1-fixed-step` 两类任务重新设计：**同类首题探索，形成参数化 SOP / SKILL，后续题沿用同一机制，只替换参数。**

默认种子包含 **12 个类型 × 每类 3 题 = 36 题**，其中 6 类 API、6 类部署。
[题组覆盖表](suites/20260918/COVERAGE.md) · [赛制研究与设计依据](RULES_ANALYSIS.md)

上一版 34 道独立变体已移到 `legacy-independent/`，仅作为历史压力测试保留，不再是本目录默认题库。原 `1-fixed-step` 和仓库解题代码未修改。

## 出题方式

| 层级 | 固定内容 | 变化内容 |
|---|---|---|
| 同一 API 类型的任务 1、2、3 | 同一服务地址、端点、认证与凭据、查询参数、分页、响应字段、统计口径、提交格式 | 查询地区/医院/电站/馆/仓库/流域，以及对应记录和答案 |
| 同一部署类型的任务 1、2、3 | 配置格式、目录布局模式、故障类别、权限策略、验收入口、凭据字段 | 工作区、应用名、配置路径中的应用参数、目标端口、密钥内容、本题凭据 |
| 不同类型之间 | 三题递进的出题结构 | API 协议、业务主题，或部署格式、修复规范等机制 |

任务 1 给出探索入口并提示整理可复用流程；任务 2、3 明确说明与首题同类、哪些参数改变。后续题不暗中更换协议或新增未说明的机制。
文档一直可读，SOP 复用是提速手段，不是禁止重新读取文档。

## 文件结构

```text
lab.py                         分组生成器、持久沙盒、连续回合驱动、CLI
engine.py                      HTTP 模拟、文件状态判题与参考工具
selftest.py                    复用同一参考脚本完成三题的自检
driver_smoke.py               现有解题器的双类型交错协议测试
suites/20260918/
  manifest.json                题组列表、示例双任务点顺序
  api_heritage/
    group.json                 组织方：固定机制、可变参数、预期可复用流程
    public/API_DOCS.md         三题共用的一份文档
    task_1/public/task.md      首题：探索
    task_2/public/task.md      次题：复用
    task_3/public/task.md      三题：复用
    task_N/private/case.json  组织方数据和判题依据
  deploy_conf/
    group.json
    task_N/public/             本题题面、spec 和补充文档
    task_N/private/case.json  初始故障和验收规则
  ...
```

`public` 中的 `{{BASE_URL}}`、`{{WORKSPACE}}`、`{{DOCS}}` 和 `{{SKILLS}}` 在发题时替换为真实地址。
组内 API 只启动一次，三题地址不变。每次完整实验重新选择空闲端口，避免与 8899 冲突。
沙盒内的 `skills/` 初始为空，三题之间不清空；Agent 可自行保存 SOP、SKILL、参数化脚本。部署工作区也保留至整组结束。组织方不会预先给真实模型注入参考解法或 SOP。

## 手动按顺序体验一组

```bash
python3 /tmp/selfEvolutionTask/2-generalization/lab.py list
python3 /tmp/selfEvolutionTask/2-generalization/lab.py serve-group --group api_heritage
# 或
python3 /tmp/selfEvolutionTask/2-generalization/lab.py serve-group --group deploy_release
```

保持进程运行，在另一个终端读取打印出的实际题目路径并操作。
原终端输入一行 JSON 答案进行判题；通过后自动发任务 2，再发任务 3。
`skip` 模拟失败结束并发下一题，`quit` 退出。单题失败不会清空已形成的技能。
手工模式不等待真实时间的冷却，不模拟地图移动和战斗。退出整组后临时沙盒删除，如需保留自行保存文件。

## 连续运行当前 Agent

```bash
# 需要 shell 已配置 DASHSCOPE_API_KEY；此命令会调用真实模型。
python3 /tmp/selfEvolutionTask/2-generalization/lab.py run-group \
  --group api_heritage --model qwen3.6-27b \
  --output /tmp/selfEvolutionTask/2-generalization/reports/api-heritage.json

# 两个类型交错：A1 → B1 → A2 → B2 → A3 → B3
python3 /tmp/selfEvolutionTask/2-generalization/lab.py run-pair \
  --pair api_heritage deploy_conf \
  --output /tmp/selfEvolutionTask/2-generalization/reports/two-points.json
```

可传 `--repo <仓库绝对路径>`、`--max-rounds 40`、`--seed <种子>`。
默认仓库为 `/Users/madison/Desktop/Huawei-Competition/Coding-Competition`。

驱动直接复用仓库 `PioneerTaskSolver`、`MatchState` 和原 `run_platform_command`，保持同一解题器实例、状态目录与单调递增回合；两类交错时也使用同一解题器。组内服务和技能文件不重置。真实提交反馈会被解题器消费，随后任务清空并进入下一题。

本驱动专门测量自进化解题链路，不运行 GameServer 的移动、经济、回防和战斗策略，以免将这些活动计为 SOP 效率。虚拟时钟按每个类型独立维护任务结束后的 30 回合冷却，跳过空闲等待，冷却不计入该题解题回合。失败或超时后仍继续后续题。
原 `tools/run_local_task.py --fixtures` 按固定城市和工作区识别旧题，不能直接使用新版目录；请使用上述分组入口。

## 如何判断发生了自进化

报告逐题记录：

- 正确性、实际解题回合、LLM 调用次数、沙盒调用次数、真实 HTTP 请求数、错误提交次数。
- 每题前后 `skills/` 文件摘要，以及当前解题器自身的 experience/metrics；报告旁保存最终经验文件和技能产物。
- 任务 2、3 对任务 1 的回合节省、LLM 调用节省；失败任务不计算提速，差值允许为负。
- 参考速度奖励 `5 × timeoutRounds / 解题回合`，用于理解短回合的价值，不冒充官方全局得分。

后续题更快是要检验的结果，环境不会硬编码为成功，也不会给任务 2、3 人工减少计时。不同题数据略有差异，这些对比不等同于严格同题的冷启动/热启动因果实验。若只看到更快但没有经验或技能证据，不应据此宣称学习机制已得到验证。

## 自检与生成新实例

```bash
# 不调用真实模型；用同一份参考参数化脚本连续完成三题。
python3 /tmp/selfEvolutionTask/2-generalization/lab.py selftest
# 固定回调 + 现有解题器，两类交错六题。
python3 /tmp/selfEvolutionTask/2-generalization/driver_smoke.py

# 新种子改变数据、应用目标值和凭据，不改变组内三题机制。
python3 /tmp/selfEvolutionTask/2-generalization/lab.py generate --seed 20260919
python3 /tmp/selfEvolutionTask/2-generalization/lab.py selftest --seed 20260919
```

扩展新类型应增加一个完整三题组：先定义共同机制，再生成三个参数实例，不把一次性的协议突变伪装成后续复用题。

## 解释范围

所有数据为本地合成；官方并未公开完整类型清单，本库的 12 类和各类恰好三题是实验设计，不是官方承诺。
环境按完整答案判通过，部署独立复检真实文件并拒绝篡改验收脚本；暂不模拟部分完成的积分结算。`score_reward=50`、`timeout_rounds=40` 等为实验参数。

自检中的参考 API 程序由组织方已知协议构造，部署程序读取公开 spec；其用途是证明一份流程确实能解三题。它们不是模型自行探索产生的技能。固定回调测试也只验证协议与状态连续性，不代表真实 LLM 的成功率或提速。

沿用现有宿主机子进程运行方式，不是隔离的对抗判题环境；private 数据不作为任务输入，但没有容器级隐藏。本地题目服务不访问外网；实际模型调用由组织方驱动发起。真实比赛沙盒无法访问外网的规则见任务书。
