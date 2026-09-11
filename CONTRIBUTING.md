# 多人协作开发准则

适用范围：本仓库全部代码、测试、文档和自动化配置。默认面向 2–6 人比赛团队。

## 1. 协作原则

- `main` 始终保持可以安装、启动、通过测试；所有修改通过 Pull Request（PR）合并。
- 一个 Issue 对应一个明确目标、一个负责人和可验证的验收条件；一个 PR 只解决一个目标。
- 开始前认领 Issue，写清预计修改的函数/文件、依赖任务和验收方法；有交叉时先协调。
- 每个 PR 至少由一位非作者成员审查；作者处理反馈，维护者确认后合并。
- 建议普通 PR 控制在约 400 行有效代码变更以内；较大改动按接口、实现、接入拆分。

## 2. 分工与目录

当前保留 `main.py` 单文件交付方式，不在协作规范引入时重构运行代码。

- 接口与状态：SDK 入口、`callback`、`MatchState` 和字段解析。
- 策略：寻路、经济、建造、战斗、商店道具，各任务明确负责的函数。
- 校验与持久化：`BasicActionValidator`、跨回合记忆、日志与异常恢复。
- 测试与交付：`tests/`、`tools/`、CI 和比赛环境验证。
- `docs/`：规则依据、设计决策、已验证事实及待确认事项。
- `results/`：只提交精选、脱敏且能说明结论的验证记录。
- `logs/`、`state/`、缓存和虚拟环境：本地运行产物，不进入版本控制。

多人改 `main.py` 时，在 Issue 标明函数范围。全文件格式化、公共数据结构修改和函数移动必须单独安排，避免与功能开发同时进行。

后续模块化建议：保留 `main.py` 作为入口，将状态解析、策略、指令校验、持久化逐步迁入 `bot/state.py`、`bot/strategy/`、`bot/validation.py`、`bot/persistence.py`。先确认判题平台打包方式，再逐步迁移；每一步保持原行为并更新交付清单。以上目录是规划，尚未创建。

## 3. GitHub 连接与成员权限

现有远程：`https://github.com/wangpei066-hue/Coding-Competition.git`。

管理员邀请成员使用各自 GitHub 账号，开发者授予 Write 权限；只给必要维护者 Admin 权限。禁止共享账号、访问令牌或 SSH 私钥。HTTPS 通过凭据管理器登录，或使用个人 SSH 密钥，令牌不得写入远程 URL。

新成员执行：

```powershell
git clone https://github.com/wangpei066-hue/Coding-Competition.git
cd Coding-Competition
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

优先安装比赛指定的 Python 3.11.10；用 `python --version` 确认创建虚拟环境所用版本。已克隆的成员先用 `git remote -v` 检查连接，不重复初始化仓库。

## 4. 分支与日常流程

采用 `main` + 短期任务分支，不设长期 `develop` 分支。命名：`feat/12-night-targeting`、`fix/23-state-reset`、`docs/31-api-notes`、`refactor/42-parser`、`chore/51-ci`。

开始新任务前确保工作区干净；已有改动先提交到所属任务分支或自行暂存，禁止直接丢弃。

```powershell
git switch main
git pull --ff-only origin main
git switch -c feat/12-night-targeting
# 修改代码、补充测试后
git diff
git add main.py tests/test_v1_strategy.py
git commit -m "feat(strategy): 优先集火低血量目标"
git push -u origin feat/12-night-targeting
```

在 GitHub 创建目标分支为 `main` 的 PR，填写模板，通过 `Closes #12` 关联任务。尚未完成时标记 Draft。

分支开发期间同步主线：

```powershell
git fetch origin
git merge origin/main
# 如有冲突，与相关作者确认后解决
git add <已解决的文件>
git commit
```

最后两行仅用于需要手动解决的冲突；无冲突时 Git 通常自动完成合并。解决后重新运行测试，再 push。不要盲目选择全部 ours/theirs，不对共享分支强推。

提交格式：`类型(范围): 简短说明`，类型使用 `feat`、`fix`、`refactor`、`test`、`docs`、`chore`。合并使用 Squash and merge，PR 标题遵循同样格式，合并后删除远程任务分支。

## 5. 代码与接口约定

- Python 使用四空格缩进、UTF-8、LF；函数/变量用 `snake_case`，类用 `PascalCase`，常量用 `UPPER_SNAKE_CASE`。
- 修改沿用周围风格；新增公共函数标注类型，复杂规则解释原因与依据，避免无关重排。
- 保持 `POST /` → JSON 对象 → `callback(json_data)` 返回 dict 的 SDK 契约；不得随意改字段名、指令 schema 或启动参数。
- 公共接口变更先在 Issue 写明输入、输出、默认值、异常、兼容性和调用示例，让受影响成员确认。
- 规则依据优先查阅 `docs/接口文档.md`、`docs/任务书.md` 和 `docs/rules_verified.md`；未经真实验证的假设明确标注，不作为官方结论。
- 新依赖写入 `requirements.txt` 并固定版本，说明用途与 Python 3.11.10 兼容性；禁止依赖开发机绝对路径。
- 持久化变更考虑旧状态兼容、重启恢复和写入失败；测试使用临时目录，不污染真实对局状态。
- 不提交密码、令牌、私人对局数据和大体积原始日志。需要共享输入时提交脱敏的最小 fixture，并标明人工/官方来源。

## 6. 测试与 PR 验收

运行 `python -m unittest discover -s tests -v`。新增行为覆盖正常路径和关键边界；修复缺陷加入能复现问题的回归测试。纯文档修改无需新增测试。

审查者重点检查接口兼容、动作合法性、角色冲突、状态隔离、规则依据和失败恢复。策略收益另附相同输入/种子下的前后对比；单元测试通过不代表胜率提升。

完成条件：验收项满足、相关测试通过、文档同步、无未解决审查意见、至少一位非作者批准、GitHub 必需检查通过。

## 7. GitHub 管理员配置清单

本地文件不会自动启用远程分支保护。管理员需在仓库 Settings 中完成：

1. General 中启用 Squash merging，并启用合并后自动删除分支。
2. 为 `main` 设置分支保护规则或 ruleset：必须经 PR 合并、至少 1 人批准、新提交使旧批准失效、合并前解决全部对话。
3. 工作流首次成功运行后，将 `Python tests` 设为必需状态检查，并要求分支与主线保持最新；不设置路径过滤以免检查一直等待。
4. 禁止主线强推和删除，将限制覆盖管理员，限制绕过权限。
5. 在 `.github/CODEOWNERS` 填入真实且具有仓库写权限的成员/团队，再启用代码所有者审批；当前文件只有示例，不会自动指派人。
6. 建立 Issue 标签：`bug`、`feature`、`docs`、`priority:high`、`blocked`；可选 Projects 看板：待办、开发中、审查中、完成。

分支保护的可用性取决于仓库可见性和 GitHub 套餐；界面不可用时管理员应确认套餐支持，未配置前不能宣称已强制执行。

官方依据：[受保护分支](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)、[PR 审查](https://docs.github.com/en/pull-requests/reference/pull-request-reviews)。

## 8. 发布与回滚

比赛提交前冻结新功能，在 Python 3.11.10 下跑完整测试、启动验证和必要的真实对局验证；记录 commit SHA、依赖版本、配置和已知限制。由维护者为确认的提交打 `v1.1.0` 等标签，提交包只包含运行必需文件。

线上/比赛问题优先通过 `git revert <有问题的合并提交SHA>` 在修复分支生成撤销提交，再走 PR 和测试；不通过重写 `main` 历史回滚。当前使用 squash，因此撤销对应的 squash 提交即可。
