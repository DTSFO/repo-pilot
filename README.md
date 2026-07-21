# RepoPilot

RepoPilot 是一个**模型驱动、证据优先且受 Harness 约束的有界多角色仓库研究 Agent**。它摄取代码仓库，接收自然语言研究目标，让 Planner、Researcher、Reviewer 和 Writer 在同一条持久化状态机中协作，最终输出每条仓库结论都带 `路径:行号` 可解析引用的报告；没有足够证据时明确拒绝推断。

它不是四个可以任意对话或执行命令的自治 Agent。模型负责结构化规划、只读工具选择、语义审核和证据内写作；代码负责 Schema、工具白名单、预算、引用、恢复和降级等不可放宽的边界。默认确定性模式不需要 API，真实 Provider 模式可连接任意 OpenAI-compatible 接口。

## v1.1 核心能力

- **有界多角色状态机**：Planner → Researcher → Reviewer → Writer；证据缺口允许 Reviewer 把附加查询退回 Researcher，但只执行配置允许的额外轮数。
- **LLM Planner + Schema 校验**：模型生成结构化查询、子问题和完成标准；非法输出、Provider 错误或 fallback 会使用确定性本地计划，并传播 `degraded=true`。
- **受控 Researcher Harness**：模型只能选择注册表中的只读仓库搜索/读取工具；参数经 JSON Schema 校验，并受步骤、工具调用、Token、超时、重试和重复调用预算限制。
- **双层 Reviewer**：代码先执行来源存在性、去重、分数、覆盖率和引用可解析性硬校验；模型再做相关性/蕴含审核，但只能缩小硬规则通过的集合，不能提升硬规则拒绝的证据。
- **受约束 Writer**：只接收最终 `accepted evidence`；输出后验证 `[n]` 引用，出现无引用、越界引用、Provider fallback 或生成失败时丢弃叙述并降级为 evidence-only 报告。
- **完整状态恢复**：Checkpoint 保存下一节点、计划、候选证据、审核结果、返工轮次、预算计数和降级原因；恢复从节点/轮次继续，Evidence 以最终审核快照幂等替换。
- **Provider 弹性层**：OpenAI-compatible 客户端提供超时、指数退避、熔断与确定性 fallback；fallback provenance 会贯穿 Runtime、Trace 和最终任务状态。
- **可复现离线模式**：确定性 Planner/Researcher/Reviewer/Writer 复用同一状态机、证据规则、Checkpoint 和 API 契约，测试不依赖实时模型。

## 快速开始

```bash
uv sync --all-extras

# 离线模式（默认，无需任何 API）
uv run repopilot serve --port 8000

# 摄取当前工作区并提问
curl -X POST localhost:8000/api/ingest -H 'content-type: application/json' -d '{}'
curl -X POST localhost:8000/api/tasks -H 'content-type: application/json' \
  -d '{"goal": "tool retry 逻辑在哪里，如何限制重试？"}'
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
| POST | `/api/ingest` | 摄取工作区（拒绝路径穿越，跳过二进制/超大文件） |
| GET | `/api/search?q=` | 仓库检索，返回带 `路径:L行` 引用的命中 |
| POST | `/api/tasks` | 创建研究任务（202 异步受理） |
| GET | `/api/tasks/{id}` | 任务状态、降级标记与最终报告 |
| GET | `/api/tasks/{id}/events?after=` | 增量事件查询 |
| GET | `/api/tasks/{id}/stream` | SSE 实时流（历史回放 + `Last-Event-ID` 续传） |
| GET | `/api/tasks/{id}/evidence` | 最终审核证据链（accepted/rejected） |
| POST | `/api/tasks/{id}/resume` | 从最新 WorkflowState Checkpoint 恢复 |
| POST | `/api/tasks/{id}/cancel` | 取消运行中任务 |
| GET/POST | `/api/memory` | 跨任务记忆查询 / 手动写入 |
| GET | `/health` `/ready` | 存活 / 就绪探针 |

设置 `REPOPILOT_API_TOKEN` 后，所有 `/api/*` 需要 `Authorization: Bearer <token>`。

## CLI

```bash
uv run repopilot serve [--host --port]   # 启动 API
uv run repopilot ingest [--path 子目录]  # 命令行摄取
uv run repopilot eval                    # 跑固定离线评测并写 evals/report.json
```

## 评测口径

v1.0 的冻结离线基线仍保留在 `evals/baseline.json`，用于历史对照：30 个固定用例、任务成功率/Recall@5/引用可解析率/拒答准确率均为 1.0。该版本中的 `unsupported_claim_rate` 由拒答准确率派生，**不是逐主张 groundedness 指标**，因此不能据此宣称事实准确率或“解决幻觉”。

v1.1 的发布评测除保留检索与拒答指标外，还应分别记录引用有效率、逐主张支持率、语义审核质量、返工成功/触顶率、fallback 率和 degraded 率。离线基线只能证明确定性语料和状态机的可复现性，不能代替真实 Provider 的模型质量或线上容量基准。

## 测试与质量门禁

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=repopilot --cov-branch
```

关键行为测试包括：Planner 结构化输出与回退、模型工具调用、未知/失败工具隔离、Reviewer 硬门槛与语义缩小、返工轮次上限、Writer accepted-evidence 隔离与引用后验校验、fallback → degraded、节点级恢复、Evidence 幂等、提示注入不能扩大工具权限，以及 SSE 断线回放。Web UI 另使用 Playwright 在桌面和移动视口执行真实任务冒烟。

## 架构

```text
FastAPI (REST + SSE)
  └─ TaskService（事件流 · WorkflowState Checkpoint · 恢复/取消）
       └─ ResearchWorkflow
            ├─ Planner: LLM structured plan → schema/hard limits → deterministic fallback
            ├─ Researcher: model tool choice → read-only ToolRegistry → repository evidence
            ├─ Reviewer: deterministic hard gate → LLM relevance/entailment review
            │              └─ evidence gap → Researcher（bounded additional rounds）
            └─ Writer: accepted evidence only → citation validation → evidence-only fallback
  ├─ HybridRetriever（BM25 + CJK bigram + snake_case subtokens + weak hash bonus）
  ├─ ModelProvider（Resilient → OpenAI-compatible / deterministic）
  ├─ RepositoryIngestor（安全遍历 → 版本化文档 → 行号分块）
  └─ SQLite/SQLAlchemy（tasks, events, checkpoints, chunks, evidence, memory, eval runs）
```

详见 `docs/ARCHITECTURE.md`、`docs/PRODUCT_SPEC.md`、`docs/ACCEPTANCE.md` 和 `docs/RELEASE_GATES.md`。

## 完整性与已知边界

RepoPilot v1.1 的“完整”指单用户、自托管、代码仓库只读研究这一产品范围：摄取、规划、工具研究、证据审核、有界返工、引用写作、恢复、API、UI、MCP、评测与容器交付形成闭环。它不等于企业级多租户或互联网规模生产系统。

- 检索仍以 BM25 为主，确定性哈希嵌入只提供弱余弦加成；尚未接入学习型语义 embedding/reranker。
- Researcher 仅有已注册的只读仓库工具，没有 Shell、代码写入、网页抓取或任意文件系统权限。
- SSE 使用数据库短轮询并面向单副本语义；多副本部署需要持久队列和发布/订阅层。
- Checkpoint 提供节点/轮次级 WorkflowState 恢复，不承诺远端模型请求的字节级重放；语料漂移会显式降级并重新检索。
- Reviewer/Writer 的模型判断降低无依据输出风险，但不等于形式化证明；高风险结论仍需要人工复核和专项对抗评测。
- 当前冻结数字是离线固定语料结果；未经过容量测试时不宣称线上吞吐、并发能力或真实模型质量。

## Docker

```bash
docker compose up --build
curl localhost:8000/ready
```
