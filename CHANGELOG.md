# Changelog

## 1.3.0 — 2026-07-22

- 将 OpenAI-compatible 调用默认切换为上游 SSE，并在 Provider 内缓冲、合并和校验文本、
  tool calls、finish reason、served model 与 usage；对外仍返回完整 `ModelResponse`，不暴露未
  校验的 Planner/Reviewer JSON 碎片或 Writer 草稿。
- 增加 started/first-byte/progress/completed/retry/timeout/failed/cancelled 生命周期事件、
  TTFT/请求延迟指标、分阶段 HTTP 超时和安全 telemetry allowlist；SSE transport heartbeat
  与持久化 Provider progress 使用不同语义。
- 在 Provider 不返回 usage 时采用明确标记的保守字符估算，避免全局 Token 预算被错误记为
  0；tool/Token 预算现在跨全部 Researcher 返工轮次累计并在模型调用前拦截。
- Reviewer 获得完成标准、已执行查询和候选证据上下文；返工查询做新颖性去重，并以证据增量、
  缺失要求、停滞和协议校验决定是否继续。预算耗尽、返工上限或停滞以 `guarded` 诚实终止。
- 加固同进程任务事件序号并发分配、SSE heartbeat/重放、Provider 事件 ContextVar 隔离和 UI
  状态展示；仍不宣称跨副本 pub/sub、in-flight replay 或 exactly-once。
- 上游 SSE 现在拒绝无 `[DONE]`/terminal finish reason 的提前 EOF；usage 归一化、熔断 open
  路径、fallback/cancel 终态和安全日志均补齐回归测试。
- Web UI 移除不可信 `innerHTML`，改用 `textContent` 和可携带内存态 Bearer Header 的 Fetch
  Streams SSE；Token 不进入 URL 或浏览器持久存储。
- 发布门禁独立打开并校验 wheel/sdist、包元数据、危险成员和高置信 secret pattern；CI 增加
  双构建复现、clean-wheel smoke 与 hardened Docker/Compose smoke。
- 修复并发 resume 可重复启动同一任务、半开熔断探针取消后永久占用许可、重试退避取消缺少
  terminal 事件，以及显式文件摄取绕过目录安全过滤的发布阻断问题。
- 检索 Top-K 先按来源多样化再按分数回填，避免单个大文件的重叠 chunk 垄断证据槽位；最终
  30-case deterministic 回归的 task success 与 Recall@5 均为 `1.0`。

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
