# 官方新闻与民间传闻

任务书把世界新闻分成两类：**官方消息**（矿价/停工）和 **民间传闻**（祭坛宝藏）。  
策略只产出标准 JSON，写入 `NewsMemory` 并打 `NEWS_INFER` 日志。当前默认**不**据此指挥工人采矿或开拓者 `summonTreasure`；接线时用本文「代码里怎么拿」。

落盘：`state/news_memory.json`（字段 `officialPlan` / `folkPlan`）。

## 怎么搜日志

平台下载的是 stderr 一行 JSON：`marker` → `event` → `roundNo` → `title` → 其余。  
搜 `"marker":"NEWS_INFER"`，再筛：

| `event` | 看什么 |
| --- | --- |
| `official_ingested` | 新官方消息原文 |
| **`official_plan`** | 官方矿价 **`plan`**（工人） |
| `folk_ingested` | 新传闻原文、累计 `legends` |
| **`folk_plan`** | 传闻宝藏 **`plan`**（开拓者） |
| `prompt_sent` | `consumer` 为 `ore` 或 `treasure`，完整 `promptText` |
| `llm_output` | `parsedJson` 与落地后的 `plan` |

真正给策略用的是 **`plan`**。`title` 只是中文摘要。

有矿价效应或已有传闻 JSON 时，每回合结束还会再打一行当前 `official_plan` / `folk_plan`。

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

| 字段 | 含义 |
| --- | --- |
| `today` | 当前游戏日（`roundNo // 130 + 1`） |
| `oreEffects` | 各矿种完整日程（启发式或 LLM） |
| `bannedOres` | **今天**不能采：`iron` / `copper` / `stone` |
| `stockpileOres` | **今天**该抢收（明天开始禁采） |
| `priceUpOres` | **今天**回收价上涨的矿 |

`oreEffects[]`：`affectedOre`、`mineBannedDays`、`priceUpDays`、`source`（`heuristic` / `llm`）、`publishedDay`、`notes`。

`bannedOres` / `stockpileOres` / `priceUpOres` 是按 `today` 从 `oreEffects` 切出的**当天快照**。同一份日程：

- 第 1 天：`stockpileOres: ["iron"]`，禁采/涨价为空  
- 第 2、3 天：`bannedOres` 与 `priceUpOres` 为 `["iron"]`  
- 第 4 天起：三个都空  

启发式未命中且尚无 LLM 时 `oreEffects` 为空，回合末可能不打 `official_plan`。

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

| 字段 | 含义 |
| --- | --- |
| `ready` | 是否可执行。无祭坛、无物品或 `confidence < 0.7` 会被打成 `false` |
| `confidence` | 0–1 |
| `altarPos` | 祭坛 `{x,y}`，没有则 `null` |
| `items` | 献祭用品英文名（已滤掉药/券/召唤令） |
| `openFromRound` / `openToRound` | 开启窗口（回合号），未知为 `null` |
| `notes` | 依据或缺什么 |
| `source` | 目前只有 `llm` |

## 代码里怎么拿

每回合 `server.py` 已挂上 `state.news_memory`。工人请用 `worker_json(round_no)`（按当天重算）；开拓者用 `pioneer_json()`。不要只读跨天前写入的缓存。

```python
memory = getattr(state, "news_memory", None)
if memory is None:
    official, folk = {}, {}
else:
    official = memory.worker_json(state.round_no)  # bannedOres / stockpileOres / priceUpOres
    folk = memory.pioneer_json()                   # ready / altarPos / items / ...
```

工人只关心今天也可以：

```python
from src.agent.news_memory import game_day

day = game_day(state.round_no)
banned = memory.banned_ores(day)          # set，如 {"iron"}
stockpile = memory.ores_to_stockpile(day)
price_up = memory.price_boosted_ores(day)
```

开拓者现成判断在 `src/agent/treasure.py`：`hypothesis_actionable(memory)`、`altar_pos(memory)`、`hypothesis_items(memory)`。阈值 `TREASURE_ACT_CONFIDENCE = 0.7`。

等价落盘缓存：`memory.data["officialPlan"]`、`memory.data["folkPlan"]`。

相关实现：`src/agent/news_memory.py`、`src/agent/prompt_router.py`、`src/agent/news_logging.py`。平台分类日志总表见 [`logging.md`](logging.md)。
