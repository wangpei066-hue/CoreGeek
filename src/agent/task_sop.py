"""供平台 LLM 复用的部署/文件修复规则；具体参数始终来自当前任务。"""

DEPLOYMENT_SOP = r'''
部署修复规则：
1. 先从当前任务、已读文档和 spec/API_DOCS 中提取：工作区、目标文件/目录、精确权限、配置物理行号或键名、验证命令、最终提交格式。信息不足先 read 指定文档；不要凭经验猜路径、TOKEN、接口或字段。
2. execute 尽量一次完成修复和验证：set -eu；确认工作区存在；创建题目允许创建的目录/文件；修改配置；chmod 精确权限；回读断言；运行题目指定 check/测试；最后输出完整成功证据和 TOKEN/答案。不要修改 check 绕过验证，不启动无关服务，不安装依赖。
3. 沙盒通常有 sh 和常见 POSIX 命令，python 可能可用但不要依赖它。优先用 mkdir、chmod、cp、mv、sed、awk、grep、test、stat、find、printf。复杂文本修改先写临时文件并 mv 原子替换；按物理行修改时保留其他行，缺行是否补齐以题目为准。
4. 推荐 execute 形态：
   set -eu
   cd "$WORKSPACE"
   # 修复...
   # 回读校验，每个目标都 test/grep/stat
   ./check
   # 若输出含 TOKEN 或答案，保留原样输出，下一轮直接 submit
5. 首次 check 失败后只根据真实错误集中修复，不重复无意义探查。若命令超时或输出截断，下一步先缩小验证范围或读取关键文件/日志。最终验证成功后必须立即 submit 真实 TOKEN/答案；若答案要求 JSON，把 JSON 序列化成 taskAnswer 字符串。
'''
