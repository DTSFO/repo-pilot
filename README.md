# RepoPilot

[![CI](https://github.com/DTSFO/repo-pilot/actions/workflows/ci.yml/badge.svg)](https://github.com/DTSFO/repo-pilot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB)
![License](https://img.shields.io/badge/License-MIT-green)

RepoPilot 是一个**模型驱动、证据优先且受 Harness 约束的有界多角色仓库研究 Agent**。它摄取代码仓库，接收自然语言研究目标，让 Planner、Researcher、Reviewer 和 Writer 在同一条持久化状态机中协作，最终输出每条仓库结论都带 `路径:行号` 可解析引用的报告；没有足够证据时明确拒绝推断。

它不是四个可以任意对话或执行命令的自治 Agent。模型负责结构化规划、只读工具选择、语义审核和证据内写作；代码负责 Schema、工具白名单、预算、引用、恢复和降级等不可放宽的边界。默认确定性模式不需要 API，真实 Provider 模式可连接任意 OpenAI-compatible 接口。

## v1.4 核心能力

- **持久化多仓库工作区**：仓库注册表保存本地路径或 Git HTTPS 来源；每次索引生成不可变
  revision，任务、文档、Chunk、证据、Checkpoint 和记忆都按仓库/revision 隔离。切换仓库不需要
  修改环境变量或重启服务，刷新失败也不会替换最后一个 ready revision。
- **安全报告产品化**：报告 API 同时返回原始 Markdown 与服务端净化后的 HTML；UI 使用安全 DOM
  渲染，另提供 Markdown、离线自包含 HTML 和结构化 JSON 下载。导出不含脚本、CDN、Provider
  密钥或原始模型遥测正文。

- **真实 LangGraph 编排**：默认执行路径由 `StateGraph` 显式编译，拓扑为 Planner → Researcher ⇄ Reviewer → Writer；条件边只负责证据缺口返工与终止，不替角色内部模型做工具决策。
- **角色内模型驱动循环**：Planner、Reviewer、Writer 执行受 Schema/证据契约约束的模型回合；Researcher 在步骤、Token、超时和调用预算内自行选择注册的只读工具，并把 observation 送回模型继续判断。
- **LLM Planner + Schema 校验**：模型生成结构化查询、子问题和完成标准；非法输出、Provider 错误或 fallback 会使用确定性本地计划，并传播 `degraded=true`。
- **受控 Researcher Harness**：模型只能选择注册表中的只读仓库搜索/读取工具；参数经 JSON Schema 校验，并受步骤、工具调用、Token、超时、重试和重复调用预算限制。
- **双层 Reviewer 与收敛控制**：代码先执行来源存在性、去重、分数、覆盖率和引用可解析性硬校验；模型再做相关性/蕴含审核，但只能缩小硬规则通过的集合。Reviewer 同时看到完成标准、已执行查询和候选证据；返工查询必须新颖且带未覆盖要求，无新增证据时以 `review_stagnated` 收敛。
- **受约束 Writer**：只接收最终 `accepted evidence`；输出后验证 `[n]` 引用，出现无引用、越界引用、Provider fallback 或生成失败时丢弃叙述并降级为 evidence-only 报告。
- **节点级持久恢复**：Checkpoint 保存下一节点、计划、候选证据、审核结果、返工轮次、全局预算计数和降级原因；恢复从已提交节点/轮次继续，Evidence 以最终审核快照幂等替换。它不承诺在途 Provider 请求重放或 exactly-once。
- **Provider 弹性与可诊断流式调用**：OpenAI-compatible 客户端默认使用上游 SSE，在内部缓冲并校验完整响应；提供 connect/read/write/pool 分阶段超时、指数退避、熔断与确定性 fallback，并持久化不含正文的 started/first-byte/progress/terminal 时间线。
- **任务全局预算**：Tool 与 Token 消耗跨全部 Researcher 返工轮次累计；预算在后续模型/工具调用前拦截。返工上限、停滞或预算耗尽会进入 `guarded`，不会伪装成正常完成。
- **可复现离线模式**：确定性 Planner/Researcher/Reviewer/Writer 复用同一状态机、证据规则、Checkpoint 和 API 契约，测试不依赖实时模型。

## 工程亮点与可验证结果

- 把原型主循环迁移为真实 LangGraph `StateGraph`，同时保留 Researcher 节点内的
  model → tool → observation 循环；图负责可检查的控制流，模型负责受约束的局部决策。
- 设计“确定性硬门槛 + 模型语义审核”证据链，Writer 只接收 accepted evidence；语料在
  Reviewer 与 Writer 之间漂移时清空旧证据并安全拒答，不把失效引用伪装成成功结果。
- 用单一 SQLAlchemy `TaskStore` 统一 task/event/checkpoint/evidence，支持节点/轮次恢复和
  SSE `Last-Event-ID` 续传；同进程内按 task 串行分配事件 sequence，同时明确边界不是
  跨副本事件总线、in-flight 请求重放或 exactly-once。
- 用上游 Provider SSE 的首字节和增量到达区分“模型尚未返回”和“正在接收”，并将
  TTFT、请求终态延迟、retry/timeout 与 telemetry drop 暴露为 Prometheus 指标；任务 SSE
  的 `: keep-alive` 只证明传输连接存活，不代表 Provider 有进展。
- 建立 30-case 可复现评测：expected source 使用完整仓库路径精确匹配，报告保留每个 case
  的 Top-5 来源，防止测试文件名或文档 basename 造成 Recall 假阳性。
- 交付 REST、SSE、静态 UI、MCP、离线 Provider、OpenAI-compatible Provider、wheel/sdist、
  Docker/Compose 和 CI；所有权限、预算、引用与降级语义都有自动化测试覆盖。

## 快速开始

```bash
uv sync --all-extras

# 离线模式（默认，无需任何 API）
uv run repopilot serve --port 8000

# 注册并索引一个服务端可见的仓库（路径必须在允许根目录内）
curl -X POST localhost:8000/api/repositories -H 'content-type: application/json' \
  -d '{"local_path":"/workspace/example"}'
# 返回的 repository_id 用于后续所有操作
curl -X POST localhost:8000/api/ingest -H 'content-type: application/json' \
  -d '{"repository_id":"<repository_id>"}'
curl -X POST localhost:8000/api/tasks -H 'content-type: application/json' \
  -d '{"repository_id":"<repository_id>","goal": "tool retry 逻辑在哪里，如何限制重试？"}'
```

接真实模型（可选）：

```bash
export REPOPILOT_PROVIDER=openai_compatible
export REPOPILOT_LLM_BASE_URL=https://your-endpoint/v1
export REPOPILOT_LLM_API_KEY=replace-with-a-rotated-key
export REPOPILOT_LLM_MODEL=your-model
uv run repopilot serve
```

密钥只能通过环境变量注入，不要写入 `.env`、源码、命令历史、日志、测试或报告。真实 Provider 的模型质量、延迟和吞吐必须针对所选 endpoint 单独评测。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/repositories` | 列出或注册本地/Git 仓库 |
| POST | `/api/repositories/{id}/sync` | 创建新的不可变索引 revision |
| DELETE | `/api/repositories/{id}` | 归档仓库（历史任务和报告保留） |
| POST | `/api/ingest` | 按 `repository_id` 摄取；完整同步创建新 revision |
| POST | `/api/documents?repository_id=` | 上传 UTF-8 文档并发布新的 overlay revision |
| GET | `/api/search?q=&repository_id=` | 仓库/revision 隔离检索 |
| POST | `/api/tasks` | 创建绑定仓库与 revision 的研究任务（202 异步受理） |
| GET | `/api/tasks/{id}` | 任务状态、降级标记与最终报告 |
| GET | `/api/tasks/{id}/report` | Markdown 原文和安全 HTML |
| GET | `/api/tasks/{id}/exports/{md|html|json}` | 下载报告导出文件 |
| GET | `/api/tasks/{id}/events?after=` | 增量事件查询 |
| GET | `/api/tasks/{id}/stream` | SSE 事件流（SQLite polling + `Last-Event-ID` 重放 + transport heartbeat） |
| GET | `/api/tasks/{id}/evidence` | 最终审核证据链（accepted/rejected） |
| POST | `/api/tasks/{id}/resume` | 从最新 WorkflowState Checkpoint 恢复 |
| POST | `/api/tasks/{id}/cancel` | 取消运行中任务 |
| GET/POST | `/api/memory` | 跨任务记忆查询 / 手动写入 |
| GET | `/health` `/ready` | 存活 / 就绪探针 |

设置 `REPOPILOT_API_TOKEN` 后，所有 `/api/*` 需要 `Authorization: Bearer <token>`。

## CLI

```bash
uv run repopilot serve [--host --port]   # 启动 API
uv run repopilot repository add-local /absolute/path # 注册并索引本地仓库
uv run repopilot repository add-git https://github.com/org/repo.git
uv run repopilot repository list
uv run repopilot repository sync <repository_id>
uv run repopilot ingest --repository-id <repository_id>
uv run repopilot eval                    # 跑固定离线评测并写 evals/report.json
```

## 评测口径

v1.0 的冻结离线基线仍保留在 `evals/baseline.json`，用于历史对照：30 个固定用例、任务成功率/Recall@5/引用可解析率/拒答准确率均为 1.0。该版本中的 `unsupported_claim_rate` 由拒答准确率派生，**不是逐主张 groundedness 指标**，因此不能据此宣称事实准确率或“解决幻觉”。

v1.2 的历史 LangGraph deterministic 发布评测实际运行 30 cases：任务成功率 `0.9667`、Recall@5 `0.9583`、引用有效率/精确率 `1.0`、拒答准确率 `1.0`、degraded/fallback case rate 均为 `0.0`，本机重复运行 P95 约 `0.35–0.40 s`。expected source 使用完整仓库路径精确匹配（目录标签只允许显式前缀匹配），case 结果保留 Top-5 来源，因此 `tests/test_agentic_workflow.py` 不会再被误算为 `src/repopilot/workflow.py` 命中。报告保持 schema `1.1` 兼容，并记录 `orchestrator=langgraph`。其中 `claim_support_evaluated=false`、`semantic_review_evaluated=false`：数据集没有逐 claim-citation 蕴含标签或人工 Reviewer 决策标签，相应 P/R/F1 为 `null`，不能用引用可解析率或拒答率代替。

v1.3 已在最终候选上重新执行同一 30-case deterministic 回归：任务成功率 `1.0`、
Recall@5 `1.0`、引用有效率/精确率 `1.0`、拒答准确率 `1.0`、degraded/fallback case
rate `0.0`。真实 API 验收中，窄目标按 `planner → researcher → reviewer → writer` 四次调用
无降级完成；开放多文件目标在返工上限内仍有缺口时诚实返回 `guarded`。这些结果只验证接口
兼容性、Provider 生命周期、状态机、证据与引用契约；不能据此宣称真实模型质量、线上吞吐或
容量。

## 测试与质量门禁

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=repopilot --cov-branch
```

关键行为测试包括：Planner 结构化输出与回退、模型工具调用、未知/失败工具隔离、Reviewer 硬门槛与语义缩小、返工查询新颖性/停滞收敛、跨轮次 Tool/Token 预算、Writer accepted-evidence 隔离与引用后验校验、fallback → degraded、Provider buffered SSE 与生命周期事件、节点级恢复、Evidence 幂等、提示注入不能扩大工具权限，以及 SSE heartbeat/断线回放。Web UI 另使用 Playwright 在桌面和移动视口执行真实任务冒烟。

## 架构

```text
FastAPI (REST + SSE)
  └─ TaskService（事件流 · WorkflowState Checkpoint · 恢复/取消）
       └─ LangGraph StateGraph（默认编排）
            ├─ Planner: LLM structured plan → schema/hard limits → deterministic fallback
            ├─ Researcher: model-driven tool loop → read-only ToolRegistry → repository evidence
            ├─ Reviewer: deterministic hard gate → LLM relevance/entailment review
            │              └─ evidence gap → Researcher（bounded additional rounds）
            └─ Writer: accepted evidence only → citation validation → evidence-only fallback
  ├─ HybridRetriever（BM25 + CJK bigram + snake_case subtokens + weak hash bonus）
  ├─ ModelProvider（Resilient → buffered upstream SSE / deterministic fallback）
  ├─ RepositoryIngestor（安全遍历 → 版本化文档 → 行号分块）
  └─ SQLite/SQLAlchemy（tasks, events, checkpoints, chunks, evidence, memory, eval runs）
```

详见 `docs/ARCHITECTURE.md`、`docs/PRODUCT_SPEC.md`、`docs/ACCEPTANCE.md` 和 `docs/RELEASE_GATES.md`。

## 完整性与已知边界

RepoPilot v1.4 的“完整”指单用户、自托管、多个代码仓库只读研究这一产品范围：真实
`StateGraph` 编排、角色内工具循环、证据审核与停滞收敛、全局预算、引用写作、节点级恢复、
Provider 可观测性、持久化仓库/revision 管理、渲染报告与导出、API、UI、MCP、评测与容器交付
形成闭环。它不等于企业级多租户或互联网规模生产系统。

- 检索仍以 BM25 为主，确定性哈希嵌入只提供弱余弦加成；尚未接入学习型语义 embedding/reranker。
- Researcher 仅有已注册的只读仓库工具，没有 Shell、代码写入、网页抓取或任意文件系统权限。
- Provider 默认向上游发送 `stream=true`，但在内部缓冲 delta、tool calls、usage 与终止原因，完成结构化校验后才交给角色逻辑；不会把 Planner/Reviewer JSON 碎片或未验证 Writer 草稿直接推给用户。若端点拒绝 `stream_options.include_usage` 可关闭该选项；缺失 usage 会用保守字符估算并标记 `usage_estimated=true`，不应当作 Provider 官方计费数据。
- Provider `provider.request.progress` 是持久化诊断事件，状态区分 `waiting_first_byte` 与 `receiving`；SSE `: keep-alive` 是无 ID、非持久化传输注释，只证明 RepoPilot 与客户端连接存活。
- SSE 的“实时”实现是 SQLite 事件表短轮询；`Last-Event-ID` 保证按序补发已提交事件。同任务 sequence 的并发分配只在当前进程内串行化，不是消息总线、多进程协调或多副本 pub/sub。
- Checkpoint 提供节点/轮次级 WorkflowState 恢复；它不会重放在途 HTTP/模型请求，节点边界前中断的幂等工作可能安全重启。恢复预检或 Reviewer 阶段发现语料漂移会显式降级并重新检索；若漂移恰好发生在 Reviewer 提交后、Writer 执行前，则清空旧证据、跳过模型写作并安全拒答，避免新增 Writer 回边造成活性风险。
- SQLAlchemy `TaskStore` 是任务、事件和 WorkflowState Checkpoint 的单一持久恢复真源；LangGraph 负责执行拓扑，不再配置第二套 LangGraph saver，避免 checkpoint 双写和恢复分歧。
- `degraded=true` 表示执行中发生 fallback、协议异常、语料漂移或能力降低；终态仍可能是 `completed`。`guarded` 是另一维度，表示返工上限、停滞或任务全局预算阻止继续安全完成，不能把它隐藏成正常成功。
- Reviewer/Writer 的模型判断降低无依据输出风险，但不等于形式化证明；高风险结论仍需要人工复核和专项对抗评测。
- 当前冻结数字是离线固定语料结果；未经过容量测试时不宣称线上吞吐、并发能力或真实模型质量。
- 本地路径和 Git clone 是服务端行为；浏览器提交的路径指向服务端可见文件系统，不会读取用户
  电脑的任意路径。Git 仅接受无凭证的 HTTPS URL，并在受控仓库卷中浅克隆。
- revision 是索引快照标识。刷新不会改变已运行任务的 revision；删除/重命名文件只会在新快照中
  生效，旧任务仍可复现和导出。Checkpoint 是节点/轮次恢复，不是 exactly-once。

## Docker

```bash
docker compose up --build
curl localhost:8000/ready
```

Compose 默认只绑定 `127.0.0.1` 且使用空 Token，适合本机演示。需要远程访问时，必须显式设置
`REPOPILOT_BIND_ADDRESS` 和高熵 `REPOPILOT_API_TOKEN`，并通过开启 TLS 的反向代理暴露服务；不要将无鉴权
实例直接绑定到公网或局域网地址。默认的工作区挂载是只读的，可用 `REPOPILOT_WORKSPACE`
和 `REPOPILOT_IMPORT_ROOT` 缩小到明确的导入目录。
