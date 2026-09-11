# 项目结构重组总结

按照官方示例 COREGEEK 的结构标准，项目已重新整理。

## 新项目结构

```
Competition-main/
├── main.py                    # 程序入口，启动GameServer
├── pyproject.toml             # Python项目配置
├── competition-agent.iml      # IntelliJ IDEA配置
├── requirements.txt           # 依赖列表
├── README.md                  # 项目文档
├── .gitignore                 # Git忽略配置
├── .editorconfig              # 编辑器配置
├── .github/                   # GitHub工作流
│   ├── workflows/
│   ├── CODEOWNERS
│   └── pull_request_template.md
├── .idea/                     # IntelliJ IDEA配置
│   ├── modules.xml
│   ├── workspace.xml
│   └── inspectionProfiles/
│       └── profiles_settings.xml
├── src/
│   └── agent/                 # 核心业务逻辑包
│       ├── __init__.py        # 包导出接口
│       ├── protocol.py        # 数据协议与状态定义
│       ├── grid.py            # 网格算法（寻路、碰撞检测）
│       ├── brain.py           # 策略与决策逻辑
│       └── server.py          # HTTP服务器与请求处理
├── tests/                     # 测试用例
│   ├── test_p0.py            # 集成测试
│   ├── test_state_parser.py  # 状态解析测试
│   ├── test_v1_strategy.py   # 策略测试
│   ├── test_persistence.py   # 持久化测试
│   ├── test_response_logging.py
│   ├── test_shop_items.py
│   └── fixtures/
│       ├── sample_match_state.json
│       └── valid_request.json
├── docs/                      # 文档
│   ├── 接口文档.md
│   ├── 任务书.md
│   ├── current_audit.md
│   └── rules_verified.md
├── logs/                      # 请求/响应日志（运行时生成）
├── state/                     # 跨回合持久化（运行时生成）
└── tools/                     # 工具脚本
```

## 模块划分

### `src/agent/protocol.py`
数据协议与状态定义
- `Pos`, `Zone`, `MapInfo` - 地图相关
- `Role`, `TeamOur`, `TeamEnemy`, `RobotInfo` - 角色与团队
- `PlayerTask`, `ShopItem`, `ErrorInfo`, `WorldNews` - 游戏事件
- `MatchState` - 完整游戏状态快照
- `GameState`, `Strategy`, `ActionValidator`, `TaskSession` - 抽象接口

### `src/agent/grid.py`
网格寻路与碰撞检测
- `chebyshev()` - 切比雪夫距离
- `astar_next_step()` - A*寻路算法
- `move_towards()` - 向目标移动
- `build_blocked_set()` - 构建阻挡集合

### `src/agent/brain.py`
游戏策略与决策逻辑
- `V1Strategy` - V1版本策略（白天经济+夜间战斗）
- `BasicActionValidator` - 指令校验
- 日间决策：`decide_worker_day()`, `decide_pioneer_day()`, `plan_day()`
- 夜间决策：`plan_night()`
- 子系统：道具购买、自我治疗、建造、战斗等

### `src/agent/server.py`
HTTP服务与请求处理
- `GameServer` - 游戏服务器主类
- `load_build_memory()` - 从磁盘恢复学习记忆
- `save_build_memory()` - 保存跨回合学习记忆

## 关键设计

1. **模块化分离**
   - 协议定义与业务逻辑分离
   - 算法与策略分离
   - 服务器与决策分离

2. **跨回合持久化**
   - 学习记忆（建造黑名单、任务状态等）保存到 `state/build_memory.json`
   - 支持服务重启后继续学习

3. **请求日志**
   - 每次请求/响应保存到 `logs/request_*.json` 和 `logs/response_*.json`
   - 便于调试和分析

4. **本地校验**
   - 在发送指令前进行本地合法性校验
   - 降低被判题器拒绝的概率

## 运行方式

```bash
# 启动服务（监听端口5000）
python main.py 5000

# 运行测试
pytest tests/

# 运行特定测试
pytest tests/test_v1_strategy.py -v
```

## 配置文件

- `pyproject.toml` - 项目元数据与依赖配置
- `requirements.txt` - 运行依赖（Flask等）
- `pyproject.toml` 的 `[tool.pytest]` - pytest配置
- `.editorconfig` - 编辑器风格配置

## 迁移清单

- [x] 创建 `src/agent/` 包结构
- [x] 提取 `protocol.py` - 数据定义
- [x] 提取 `grid.py` - 寻路算法
- [x] 提取 `brain.py` - 策略逻辑
- [x] 创建 `server.py` - HTTP层
- [x] 重写 `main.py` - 新入口
- [x] 创建 `pyproject.toml` - 项目配置
- [x] 更新所有测试文件导入
- [x] 创建IDE配置文件
- [x] 验证所有测试通过
