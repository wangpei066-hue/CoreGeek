# 官方新闻与民间传闻

任务书把世界新闻分成两类：**官方消息**（矿价/停工）和 **民间传闻**（祭坛宝藏）。  
策略把官方消息产出的标准 JSON 写入 `NewsMemory` 并打 `NEWS_INFER` 日志。工人经济逻辑会使用官方 `plan`：禁采日不挖对应矿，禁采/涨价前一日优先抢收，涨价日优先卖出囤货。民间传闻仍只写入宝藏 plan，开拓者是否行动看 `folkPlan.ready` 与置信度。

落盘：`state/news_memory.json`（字段 `officialPlan` / `folkPlan`）。

## 怎么搜日志

平台下载的是 stderr 一行 JSON：`marker` → `event` → `roundNo` → `title` → 其余。  
搜 `"marker":"NEWS_INFER"`，再筛：


| `event`             | 看什么                                             |
| ------------------- | ----------------------------------------------- |
| `official_ingested` | 新官方消息原文                                         |
| `**official_plan`** | 官方矿价 `**plan**`（工人）                             |
| `folk_ingested`     | 新传闻原文、累计 `legends`                              |
| `**folk_plan**`     | 传闻宝藏 `**plan**`（开拓者）                            |
| `prompt_sent`       | `consumer` 为 `ore` 或 `treasure`，完整 `promptText` |
| `llm_output`        | `parsedJson` 与落地后的 `plan`                       |


真正给策略用的是 `**plan**`。`title` 只是中文摘要。

`official_plan` / `folk_plan` 只在事件发生时打，**不**每回合重打：

- 官方原文变了（且不是「无重大新闻」）→ ingest 立刻打 `official_plan`
- 矿价 LLM 落地 → 再打 `official_plan`
- 宝藏 LLM 落地 → 打 `folk_plan`

传闻 ingest 只打 `folk_ingested`。当前 `plan` 仍每回合写入本地 `decision_*.json` 的 `newsPlans`。

## 处理顺序（送推）

每天新闻 LLM 最多 3 次，每回合 HTTP `prompt` 最多 1 条。自进化 `phaseTask` 占用时不送新闻。

- **官方**：先跑启发式。命中则写入 `officialPlan`，**不再**申请矿价 LLM。未命中则当天最多送 **1 次**矿价 LLM，且排在传闻前面。
- **传闻**：只累积原文，**不做**正则启发式；待解码时走宝藏 LLM。每天至少预留 **1 次**成功送推（最后 1 次额度不让官方占完）。

## 官方 `plan`（工人）

由 `NewsMemory.worker_json(round_no)` 生成。

```json
{
  "today": 1,
  "oreEffects": [{
    "affectedOre": "iron",
    "mineBannedDays": [2, 3],
    "priceUpDays": [2, 3],
    "source": "heuristic",
    "publishedDay": 1,
    "notes": "heuristic"
  }],
  "bannedOres": [],
  "stockpileOres": ["iron"],
  "priceUpOres": []
}
```


| 字段              | 含义                                    |
| --------------- | ------------------------------------- |
| `today`         | 当前游戏日（`roundNo // 130 + 1`）           |
| `oreEffects`    | 各矿种完整日程（启发式或 LLM）                     |
| `bannedOres`    | **今天**不能采：`iron` / `copper` / `stone` |
| `stockpileOres` | **今天**该抢收（明天开始禁采）                     |
| `priceUpOres`   | **今天**回收价上涨的矿                         |


`oreEffects[]`：`affectedOre`、`mineBannedDays`、`priceUpDays`、`source`（`heuristic` / `llm`）、`publishedDay`、`notes`。

`bannedOres` / `stockpileOres` / `priceUpOres` 是按 `today` 从 `oreEffects` 切出的**当天快照**。同一份日程：

- 第 1 天：`stockpileOres: ["iron"]`，禁采/涨价为空  
- 第 2、3 天：`bannedOres` 与 `priceUpOres` 为 `["iron"]`  
- 第 4 天起：三个都空

启发式未命中且尚无 LLM 时 ingest 仍会打一行 `official_plan`（`oreEffects` 为空，`source=pending_llm`）；矿价 LLM 落地后再打一行带效应的。

## 传闻 `plan`（开拓者）

由 `NewsMemory.pioneer_json()` 生成（`treasureHypothesis` 的拷贝）。接入当回合通常还没有；宝藏 LLM 落地后才打。

```json
{
  "ready": true,
  "confidence": 0.95,
  "altarPos": {"x": 12, "y": 12},
  "items": ["AcientTablet", "StarSand"],
  "openFromRound": 200,
  "openToRound": 260,
  "notes": "",
  "source": "llm"
}
```


| 字段                              | 含义                                             |
| ------------------------------- | ---------------------------------------------- |
| `ready`                         | 是否可执行。无祭坛、无物品或 `confidence < 0.7` 会被打成 `false` |
| `confidence`                    | 0–1                                            |
| `altarPos`                      | 祭坛 `{x,y}`，没有则 `null`                          |
| `items`                         | 献祭用品英文名（已滤掉药/券/召唤令）                            |
| `openFromRound` / `openToRound` | 开启窗口（回合号），未知为 `null`                           |
| `notes`                         | 依据或缺什么                                         |
| `source`                        | 目前只有 `llm`                                     |


## 代码里怎么拿

### 对象从哪来

每回合 `src/agent/server.py` 在调用 `strategy.decide(state)` **之前**已经执行：

```python
self.match_state.news_memory = self.news_memory
self.news_memory.ingest(self.match_state)           # 写入官方/传闻 plan
self.prompt_router.consume_llm_resp(self.match_state)  # 若有上回合 llmResp，覆盖 plan
```

因此工人/开拓者决策函数里拿到的 `state` 一定带 `state.news_memory`（类型 `NewsMemory`）。  
类定义：`src/agent/news_memory.py`。落盘文件：`state/news_memory.json`。

**不要**去读 stderr 日志当输入。日志只是同一份 `plan` 的打印。

### 工人：拿官方 plan

接到采矿/卖矿的地方，例如 `src/agent/economy.py` 的 `profitable_mine` / `sellable_ores` / `liquidate`。

```python
memory = getattr(state, "news_memory", None)
official = memory.worker_json(state.round_no) if memory else {}
# official 就是日志里 official_plan 的 plan
banned = set(official.get("bannedOres") or [])       # 今天不挖
stockpile = set(official.get("stockpileOres") or []) # 今天优先挖、先别卖
price_up = set(official.get("priceUpOres") or [])    # 今天优先卖掉
```

必须用 `worker_json(state.round_no)`，它按**本回合**重算 `bannedOres` 等三个当天数组。  
不要用 `memory.data["officialPlan"]` 当工人输入：那是上次写入的缓存，跨天后会过期。

只想要集合、不要整份 dict 时：

```python
from .news_memory import game_day
day = game_day(state.round_no)
banned = memory.banned_ores(day)            # set() 或 {"iron"}
stockpile = memory.ores_to_stockpile(day)
price_up = memory.price_boosted_ores(day)
```

空 plan（没新闻或启发式未命中）时三个集合都是空的，按原经济逻辑即可。当前接入点：

- `profitable_mine` / `opening_schedule.choose_nearest_mine`：`stockpileOres` 优先于普通铜铁；`bannedOres` 从候选矿里排除。
- `sellable_ores`：`stockpileOres` 在涨价前不卖，防止刚抢收就低价变现。
- `liquidate`：`priceUpOres` 触发优先出售，把囤货兑现。

### 开拓者：拿传闻 plan

接到 `src/agent/brain.py` 的 `decide_pioneer_day` / `decide_pioneer_task`。

```python
memory = getattr(state, "news_memory", None)
folk = memory.pioneer_json() if memory else {}
# folk 就是日志里 folk_plan 的 plan；LLM 还没回来时是 {}
ready = bool(folk.get("ready"))
items = list(folk.get("items") or [])
altar = folk.get("altarPos")          # {"x": 12, "y": 12} 或 None
window = (folk.get("openFromRound"), folk.get("openToRound"))
```

或直接用已有封装（读的是同一份 `treasureHypothesis`）：

```python
from .treasure import (
    hypothesis_actionable, altar_pos, hypothesis_items, decide_treasure_action,
)
if memory and hypothesis_actionable(memory):
    cmd = decide_treasure_action(pioneer, state, memory, blocked, reserved)
```

`folk` 为空或 `ready` 为 false 时不要买祭坛用品、不要 `summonTreasure`。置信度阈值是 `TREASURE_ACT_CONFIDENCE = 0.7`（写在 `news_memory.py`）。

`memory.data["folkPlan"]` 与 `pioneer_json()` 内容相同，开拓者可以用；没有「按当天切片」的问题。

### 和日志的对应关系


| 日志                                      | 代码取出的变量                                         | 生成函数                      |
| --------------------------------------- | ----------------------------------------------- | ------------------------- |
| `NEWS_INFER` / `official_plan` / `plan` | `official = memory.worker_json(state.round_no)` | `NewsMemory.worker_json`  |
| `NEWS_INFER` / `folk_plan` / `plan`     | `folk = memory.pioneer_json()`                  | `NewsMemory.pioneer_json` |


相关实现：`src/agent/news_memory.py`、`src/agent/prompt_router.py`、`src/agent/news_logging.py`、`src/agent/treasure.py`。平台分类日志总表见 `[logging.md](logging.md)`。
