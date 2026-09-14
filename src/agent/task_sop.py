"""供平台 LLM 复用的操作流程；题目参数仍由当前任务决定。"""

DEPLOYMENT_SOP = r'''
部署修复 SOP（文件、目录、配置、权限类任务适用）：
1. 从本题提取完整清单：工作区、目录/文件及精确权限、配置物理行号和完整内容、验证命令、提交格式。行号从1开始，空行和注释也计数。示例beta、755、行号及内容均须按本题替换。
2. 说明已读且要求完整，下一步直接execute。不要单独pwd/ls/cat逐项探查；存在性、类型和内容检查放进修复脚本。只有工作区或要求不明确时才合并一次有范围的探查/读取，不重复读取已有文档。
3. 一次execute完成：确认工作区 → 创建要求的子目录/文件 → 修改全部配置行 → 设置精确权限 → 回读逐项断言 → 运行本题验证命令。工作区必须已存在；题目要求的缺失子目录可以mkdir -p。用chmod 755而非chmod +x保证精确权限，不用chmod -R修改无关文件。
4. 配置按物理行替换，禁止插入/追加造成行号偏移，保留其他行；一次读取、修改全部目标行、一次保存。不足目标行数时补空行。缺失配置仅按题目授权创建。缺失脚本若只要求存在且可执行可创建最小shell脚本；如有功能要求须实现功能，已有脚本保留内容。
5. 可复用命令模板如下；替换路径、权限、行号和内容，按本题增删要求。放入execute.command时正确JSON转义换行，设置execute.workspace为已确认工作区。带引号的heredoc避免$、反引号等配置内容被shell展开：
set -e
python3 - <<'PY'
from pathlib import Path
import stat
root = Path.cwd()
assert root.is_dir(), '工作区不存在'
directory = root / 'logs/beta'
directory.mkdir(parents=True, exist_ok=True)
directory.chmod(0o755)
config = root / 'config/beta.conf'
assert config.is_file(), '配置缺失，须按本题要求决定是否创建'
with config.open(encoding='utf-8', newline='') as f:
    original = f.read()
lines = original.splitlines(keepends=True)
updates = {3: '替换为本题第3行完整内容', 6: '替换为本题第6行完整内容'}
newline = '\r\n' if '\r\n' in original else '\n'
while len(lines) < max(updates):
    if lines and not lines[-1].endswith(('\n', '\r')):
        lines[-1] += newline
    lines.append(newline)
for number, content in updates.items():
    assert number >= 1 and '\n' not in content and '\r' not in content
    old = lines[number - 1]
    ending = '\r\n' if old.endswith('\r\n') else '\n' if old.endswith('\n') else ''
    lines[number - 1] = content + ending
updated = ''.join(lines)
if updated != original:
    with config.open('w', encoding='utf-8', newline='') as f:
        f.write(updated)
script = root / 'bin/start.sh'
if not script.exists():
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text('#!/bin/sh\n', encoding='utf-8')  # 有功能要求时替换为真实实现
assert script.is_file(), '脚本路径不是文件'
script.chmod(0o755)
assert directory.is_dir() and stat.S_IMODE(directory.stat().st_mode) == 0o755
assert script.is_file() and stat.S_IMODE(script.stat().st_mode) == 0o755
actual = config.read_text(encoding='utf-8').splitlines()
assert all(actual[n - 1] == value for n, value in updates.items()), '配置回读不匹配'
print('文件、配置、权限逐项检查通过', flush=True)
PY
在上述命令末尾接本题指定的最终验证命令；只有本题要求./check时才运行./check。set -e确保前置失败立即停止。不执行start.sh来证明可执行权限，不启动无关服务、不安装依赖。
6. 失败时只针对真实错误集中修复并验证，不把首次check失败当作探查步骤。超时/结果缺失先核实状态。exitCode=0或局部断言通过不等于最终检查通过，须满足本题成功条件并取得真实回执。
7. 最终验证成功后，下一次响应立即submit，不再execute/read或仅回复“完成”。如要求TOKEN，提取真实值并按题目格式序列化提交，不能提交示例值。
回合目标：说明齐全后execute修复验证 → 收到结果后submit，通常2次LLM响应；不得为赶进度跳过必要验证。
'''
