# 平台下载日志

判题平台通常只能下载 **stderr JSON 行** 和沙盒 `printf`（下一回合出现在 `lastCmdResult`）。本地 `logs/decision_*.json` 更完整，但多数情况下下不到。

每行一个 JSON 对象，固定字段顺序：

`marker` → `event` → `roundNo` → `title` → 其余

`title` 是短中文摘要，下载后先看它。字符串过长会截断。保持 **一行一条**，方便 grep。

## 先搜哪个 marker

| marker | 何时出现 | 看什么 |
| --- | --- | --- |
| `STRATEGY_DECISION` | 每回合 | 昼夜、金币、武器/墙数量、告警码、各角色指令 |
| `BUILD_WEAPON` | 每回合 | 已建炮、待升级资金缺口、本回合建造/买券/开火 |
| `BUILD_WALL` | 每回合 | 一层/二层进度、缺口、本回合砌墙或采石 |
| `PIONEER_TASK` | 每回合总览；接取/解题时另有 solver 行 | `event=round` 看本回合接取/提交；`accept_requested` / `task_active` 看解题细节 |
| `ECONOMY` | **仅采矿/卖矿有动作或相关事件时** | 本回合采集/出售 |
| `NEWS_INFER` | **有新闻事件时**（不是每回合快照） | 官方消息、传闻、LLM 的 `promptText` 与 `parsedJson` |

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

## NEWS_INFER（有内容才打）

| event | 关键字段 |
| --- | --- |
| `official_ingested` | `officialNews` 全文、`oreEffect`（启发式可能为 null） |
| `folk_ingested` | `newLegend`、累计 `legends` |
| `prompt_sent` | `consumer` 为 `ore` / `treasure` / `intel`，**完整 `promptText`** |
| `llm_output` | 同一 `promptText` + `llmRespRaw` + `parsedJson` + `parseOk` / `applied` |
| `llm_empty` | 等待中的 LLM 空响应 |
| `summon_result` | `resultCode` 召唤宝藏结果 |

不要把 `world_intel` 的 ingest 再记成一遍 `official_ingested`（与 `news_memory` 重复）。情报链路只记 `consumer=intel` 的 prompt/输出。

同一回合可能先发 `intel` prompt、再申请 `treasure`/`ore`；HTTP 响应里的 `prompt` 只能带一条（自进化 > intel > news_memory）。stderr 会记下实际申请过的 prompt，下载时以 `prompt_sent` 为准。

无自进化 `phaseTask`、且本回合有新闻推断活动时，再经 `executeCmd` 的 `printf` 回传一份摘要（含 `worldNews`、`promptText`、`oreEffects`）。有自进化任务时不抢沙盒。

## 建议搜索顺序

1. `title` 或 `【武器】` / `【围墙】` / `【新闻】` / `【LLM】`
2. `"marker":"NEWS_INFER"` 再筛 `"event":"llm_output"`
3. `"marker":"BUILD_WEAPON"` 看 `thisRound`
4. `"marker":"PIONEER_TASK"` 且 `event` 为 `accept_requested` 或 `task_active`

发出接取或提交不代表成功，需对照下一回合动作结果与系统错误。
