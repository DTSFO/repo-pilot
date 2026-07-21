# Changelog

## 1.2.0 — 2026-07-21

- 将默认控制平面迁移为真实 LangGraph `StateGraph`，显式编译
  Planner → Researcher ⇄ Reviewer → Writer 拓扑和 Reviewer 条件返工边。
- 保留 Researcher 节点内部的模型驱动只读工具循环，避免把模型决策硬编码成逐工具图节点。
- 继续以 SQLAlchemy `TaskStore` 作为任务、事件与 WorkflowState Checkpoint 的单一持久恢复真源，
  不引入第二套 LangGraph saver 双写。
- 增加图拓扑、正常/返工路径、节点提交事件和无降级完整流程测试；发布评测记录
  `orchestrator=langgraph` 与图名，同时保持 schema 1.1 兼容。
- 明确 SSE 为 SQLite 事件表短轮询，Checkpoint 为已提交节点/轮次恢复，不宣称 Pub/Sub、
  在途 Provider 请求重放或 exactly-once。

## 1.1.0 — 2026-07-21

- 将研究主链升级为模型驱动、证据优先且受 Harness 约束的有界多角色状态机。
- Planner 输出结构化计划；Researcher 仅能调用注册的只读仓库工具；Reviewer 结合硬校验与语义审核，并支持有界返工。
- Writer 只接收 accepted evidence，输出后校验引用，失败时降级为 evidence-only 报告。
- 收紧真实 Provider 的 Reviewer JSON 字段类型和 Writer `[n]` 引用模板，完成正式配置下
  Planner、Researcher、Reviewer、Writer 全主模型、零 fallback、零 degraded 验证。
- 增加节点/轮次 WorkflowState 恢复、Evidence 幂等替换和 Provider fallback provenance 传播。
- 保留确定性离线模式与 v1.0 历史基线，并纠正 v1.1 评测中的逐主张指标命名。

## 1.0.0 — 2026-07-20

首个冻结版本。证据优先的仓库研究 Agent,离线全功能可用。

### 核心
- 异步 Agent Runtime:只读工具并发、单工具超时、有限重试(只读/幂等)、部分失败脱敏降级、步数/工具/Token 预算、重复调用防护、取消传播。
- 四节点研究工作流(Planner → Researcher → Reviewer → Writer),证据优先组稿,无证据拒绝推断。
- 检索:BM25(CJK bigram + snake_case 子词)× 确定性哈希嵌入余弦加成;Reviewer 相对分数 + 查询覆盖率双门槛。
- 摄取:安全遍历(拒绝穿越/符号链接/二进制/超大文件),内容哈希版本化文档,内容回退可重新成为最新版本,行号窗口分块。
- 持久化:任务/事件流/检查点/文档/Chunk/证据/记忆/评测记录(SQLite/SQLAlchemy async,PostgreSQL URL 兼容)。
- 恢复与取消:检查点恢复(含事件 `task.resumed`),执行体启动前取消也持久化终态。
- Provider 弹性:OpenAI-compatible + 指数退避重试 + 熔断 + 确定性回退;密钥仅环境变量,异常链在进入日志 handler 前脱敏。
- Memory:任务摘要自动写入,Planner 词元召回扩展查询,支持过期清理。

### 接口
- REST + SSE(Last-Event-ID 续传)、文件上传摄取、检索、证据链、记忆、健康/就绪、Prometheus /metrics。
- 响应式静态 Web UI(任务/报告/证据/实时事件),任务终态自动刷新证据链。
- 只读 MCP stdio 服务器(search_repository / read_file / get_task / list_evidence)。
- CLI:serve / ingest / eval / mcp。

### 评测(离线基线,本仓库为语料,30 用例)
- 任务成功率 1.0,Recall@5 1.0,引用精确率 1.0,拒绝准确率 1.0,无依据结论率 0.0;报告记录语料 SHA-256 指纹和实测 P95。

### 质量
- 76 项测试,分支覆盖率 85.58%(门槛 85%),ruff + ruff format + mypy strict 全绿。
- Docker 多阶段构建,非 root 运行,`/ready` healthcheck 与 Playwright 桌面/移动冒烟通过。
- MIT `LICENSE`、锁文件、wheel/sdist 和 SHA-256 发布校验和齐备。
