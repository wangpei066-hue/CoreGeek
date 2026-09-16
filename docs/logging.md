# 平台下载日志

判题平台通常只能下载 **stderr JSON 行** 和沙盒 `printf`（下一回合出现在 `lastCmdResult`）。本地 `logs/decision_*.json` 更完整，但多数情况下下不到。

每行一个 JSON 对象，固定字段顺序：

`marker` → `event` → `roundNo` → `title` → 其余

`title` 是短中文摘要，下载后先看它。字符串过长会截断。保持 **一行一条**，方便 grep。

## 先搜哪个 marker

| marker | 何时出现 | 看什么 |
| --- | --- | --- |
| `BUILD_INFO` | **进程只打一行**（启动或首回合） | `commit` 为当前 git HEAD；下载日志的第一行 |
| `STRATEGY_DECISION` | 每回合 | 昼夜、金币、武器/墙数量、告警码、各角色指令 |
| `BUILD_WEAPON` | 每回合 | 已建炮、待升级资金缺口、本回合建造/买券/开火 |
| `BUILD_WALL` | 每回合 | 一层/二层进度、缺口、本回合砌墙或采石 |
| `PIONEER_TASK` | 每回合总览；接取/解题时另有 solver 行 | `event=round` 看本回合接取/提交；`accept_requested` / `task_active` 看解题细节 |
| `ECONOMY` | **仅采矿/卖矿有动作或相关事件时** | 本回合采集/出售 |
| `NEWS_INFER` | **官方原文变化、LLM 落地、或其它新闻事件时** | `official_plan` / `folk_plan`；字段与取用见 [`news.md`](news.md) |

完整分支原因、背包明细、路径事件仍在本地 `logs/decision_NNNNNN.json`。

## 共用字段

| 字段 | 含义 |
| --- | --- |
| `marker` | 上面的分类名 |
| `event` | 行类型，如 `round` / `status` / `prompt_sent` |
| `roundNo` | 游戏回合 |
| `title` | 扫读用中文短句，例如 `【武器】已建 rocket×3 \| 本回合 10010 建造 rocket@(9,24)` |

## STRATEGY_DECISION `event=round`

金币、武器数、墙数、机器人、`alerts`（只含 code）、`roles`（id / type / status / command）。不含完整 diagnostics。

## BUILD_WEAPON `event=status`

| 字段 | 含义 |
| --- | --- |
| `standing` | 已建武器 id/类型/坐标/等级/血量/冷却 |
| `upgrade` | 待升级列表、所需金币、可用金币、资金缺口 |
| `thisRound` | 本回合建造/购买升级券/开火 |
| `events` | 相关决策分支（不含 command 副本） |

## BUILD_WALL `event=status`

| 字段 | 含义 |
| --- | --- |
| `primary` | 一层 planned/built/missing（最多 20 格）/missingCount |
| `outer` | 二层进度与 `unlocked` |
| `thisRound` | 砌墙或为墙采石 |
| `events` | 墙相关分支 |

## PIONEER_TASK

两种来源：

1. 每回合 `event=round`：本回合 `acceptTask` / `submitAnswer`、开拓者相关事件、道具任务。
2. 接取或解题时：`event=accept_requested` 或 `task_active`，带 `solverStage`、`phaseTask`、`llmResp`、`lastCmdResult`。空闲回合 **不** 打 `task_idle`。

有 `phaseTask` 且沙盒未被解题命令占用时，经 `printf` 回传原文分片（`phaseTaskChunk` / `chunkIndex`）。

## ECONOMY `event=status`

仅在本回合有采矿、卖矿或对应事件时输出。`thisRound` + `actors` 估值。

## NEWS_INFER（官方消息 / 民间传闻 两条线）

字段含义、当天三数组、以及代码里如何读取 `plan`，见 [`news.md`](news.md)。

任务书把世界新闻分成两类，日志也按两类搜：

| event | 存哪 | 看什么 |
| --- | --- | --- |
| `official_ingested` | — | 新官方消息原文 |
| **`official_plan`** | **`news_memory.officialPlan`** | **`plan` JSON**：`oreEffects`、今日 `bannedOres` / `stockpileOres` / `priceUpOres`。当前不指挥工人 |
| `folk_ingested` | — | 新传闻原文、累计 `legends` |
| **`folk_plan`** | **`news_memory.folkPlan`** | **`plan` JSON**：`ready`、`confidence`(0–1)、`altarPos`、`items`、开启窗口、`notes`。仅 LLM 写入；当前不指挥开拓者 |
| `prompt_sent` | LLM | `consumer` 为 `ore` 或 `treasure`（不再混合），完整 `promptText` |
| `llm_output` | LLM | `parsedJson` + 落地后的 `plan` |
| `llm_empty` | LLM | 等待中的响应为空 |

处理顺序：官方启发式命中则不送矿价 LLM；未命中当天最多送 1 次且优先于传闻。传闻只走宝藏 LLM，每天至少预留 1 次送推。两份 JSON 只落盘并打日志，默认不改工人/开拓者动作。详情见 [`news.md`](news.md)。

`official_plan` / `folk_plan` 只在官方原文变化（ingest）或对应 LLM 落地时打，不每回合重打。平台下载搜 `"event":"official_plan"` 或 `"event":"folk_plan"`。

不要再搜 `consumer=intel`：混合情报 prompt 已去掉。

## 建议搜索顺序

1. `"event":"official_plan"` / `"event":"folk_plan"` 看当前决策 JSON
2. `"marker":"NEWS_INFER"` 再筛 `"event":"llm_output"`
3. `"marker":"BUILD_WEAPON"` 看 `thisRound`
4. `"marker":"PIONEER_TASK"` 且 `event` 为 `accept_requested`、`task_active`

发出接取或提交不代表成功，需对照下一回合动作结果与系统错误。
