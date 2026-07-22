# RepoPilot 验收记录

## v1.4.0 发布前验收状态

日期：2026-07-22 · 范围：持久化多仓库/revision、数据库迁移、渲染报告、导出、浏览器产品流和
容器交付。v1.3.0 及更早版本的冻结数字、哈希和语义保持不变。

| 检查 | v1.4 当前实际结果 |
| --- | --- |
| 单元/集成/API/迁移/仓库安全测试 | 204 passed，1 个浏览器测试按默认环境跳过 |
| 静态与类型 | Ruff、format check、strict Mypy 全部通过 |
| 分支覆盖率 | 86.21%，超过 85% 门槛 |
| 浏览器产品流 | `REPOPILOT_RUN_BROWSER_TESTS=1 uv run pytest tests/test_browser.py`：1 passed；添加仓库→索引→任务→SSE→安全 HTML→三种下载；移动视口无横向溢出、控制台无错误 |
| v1.4 deterministic 30-case 回归 | task success 0.9667；Recall@5 0.9583；citation precision/validity 1.0；refusal accuracy 1.0；degraded/fallback 0；P95 573.341ms |
| v1.3 SQLite 迁移 | 真实旧表 fixture 升级、旧唯一约束重建、legacy repository/revision 回填、重复启动幂等、跨仓同 URI/hash 验证通过 |
| 多仓库隔离 | 两仓同名文件检索隔离；任务冻结 `repository_id/revision_id`；刷新失败保留旧 ready revision；删除/重命名只影响新快照 |
| 报告与导出 | 原始 Markdown、安全 HTML、离线 HTML、结构化 JSON；XSS/危险协议/脚本/远程依赖拒绝；运行中导出 409、未知格式 400、鉴权和下载头通过 |
| API/部署默认值 | 任务列表仅返回摘要和 `has_report`；完整报告按需读取；Compose 默认绑定 `127.0.0.1`，远程部署文档要求 Token + TLS 反向代理 |
| wheel/sdist | 两次独立构建逐字节一致；wheel `103813` bytes / `ce736feebb301827304621f4a9ef07e6b5bba8c682bbc30fbcad2c42a3c700f0`；sdist `265508` bytes / `74ea13768db325ff048b9d09f9988975ffdb1f2c1a67bab9cf3990025826ffc3` |
| clean wheel smoke | 1.4.0 元数据、CLI、静态资源、Markdown renderer 和 FastAPI 版本检查通过 |
| Docker/Compose hardened smoke | `/ready`、非 root、只读 rootfs、ALL capabilities dropped、no-new-privileges、可写 data/repository volume 通过；image `sha256:e87f7957024aacd7347fc57428f1d4b58719475d4478dd15f4cc80e2bbae1449` |

本节数字只描述当前代码、固定离线数据集和本机验收，不代表真实模型质量、线上吞吐、容量或
exactly-once。Provider 质量仍需针对具体 endpoint 单独评测；SSE 是持久事件表短轮询，Checkpoint
是节点/轮次恢复。

## v1.3.0 冻结验收状态

日期：2026-07-22 · 范围：Provider 可诊断流式调用、Reviewer 收敛控制、任务全局预算与
单进程持久事件一致性

v1.3 已在最终候选上完成代码、deterministic 评测、真实 Provider、发行包和 hardened
Docker/Compose 门禁。下方 v1.2.0 及更早版本是历史记录，其产物大小、SHA-256、镜像摘要和
评测数字保持原样。

| 检查 | v1.3 当前实际结果 |
| --- | --- |
| 单元/集成/API/发布测试 | 164 passed |
| 静态与类型 | Ruff、format check、strict Mypy 全部通过 |
| 分支覆盖率 | 87.17%，超过 85% 门槛 |
| Provider 最小真实 SSE | HTTP 200 `text/event-stream`；多次 delta + `[DONE]`；usage 可返回；`fallback=false` |
| Provider 最小生命周期 | `started → first_byte → completed`；该次 TTFT 约 1.87s、总耗时约 2.79s |
| v1.3 deterministic 30-case 回归 | task success 1.0；Recall@5 1.0；citation validity/precision 1.0；refusal accuracy 1.0；degraded/fallback 0；P95 648.53ms |
| 四角色真实 API 窄目标 | `planner → researcher → reviewer → writer`；4 calls；`completed`；`degraded=false`；fallback 0；1 条 accepted evidence；引用/final checkpoint/secret-safety 全通过 |
| 开放多文件真实目标 | 3 轮 Researcher/Reviewer；7 条 accepted evidence；fallback 0；未覆盖项在上限内未收敛，诚实 `guarded`，原因含 `review_limit_reached` |
| v1.3 wheel/sdist 与 Docker/Compose | 双构建逐字节一致、独立 archive/metadata/secret 校验、clean wheel 安装和 hardened container smoke 全通过；最终哈希见下表 |

### v1.3 已实现且必须保持的行为

- OpenAI-compatible 请求默认使用上游 `stream=true`。Provider 内部缓冲并合并文本、分片
  tool calls、finish reason、served model 与 usage，完整校验后返回单一 `ModelResponse`；
  Planner/Reviewer JSON 碎片和未验证 Writer 草稿不会作为任务输出流出。
- Provider 同时兼容“请求流式但端点返回普通 JSON”。`streaming_enabled` 与
  `stream_options.include_usage` 可独立关闭，以兼容不同 OpenAI-compatible 端点。
- connect/read/write/pool 使用独立超时；每个逻辑调用持久化 content-free 的
  `started`、`first_byte`、`progress`、`retry` 和 terminal 时间线。`progress` 区分
  `waiting_first_byte` 与 `receiving`，Prometheus 记录 TTFT、终态延迟和 telemetry drop。
- Provider telemetry 在写 TaskStore 前通过显式 allowlist；不得持久化 URL、Key、prompt、
  completion/content、token delta、response ID、原始异常文本或 tool arguments。sink 失败只
  增加 dropped counter 和安全日志，不改变推理结果。
- Provider 未返回 usage 时使用保守字符估算，并在事件中标记
  `usage_reported=false`、`usage_estimated=true`。估算只用于预算保护，不能当成官方计费量。
- Tool 与 Token 预算在同一任务的全部 Researcher/Reviewer/Writer 回合中累计；预算耗尽会
  阻止后续调用并记录 `tool_budget_exhausted` 或 `token_budget_exhausted`。
- Reviewer 输入包含 completion criteria、executed queries 和 candidates。代码控制器对
  additional queries 做 fingerprint 去重、每轮最多接受 2 条，并在缺失返工理由、重复查询、
  返工后无新增候选或达到上限时停止循环。
- `degraded` 与 `guarded` 是不同语义：fallback、协议异常或能力降低设置
  `degraded=true`，任务仍可能 `completed`；返工上限、停滞或任务全局预算耗尽使工作流以
  `guarded` 终止，不把未满足要求包装成普通成功。报告列出 Reviewer 指明的未覆盖要求。
- Provider progress 是带 sequence 的持久化 task event。SSE `: keep-alive` 是无 ID、非
  持久化 transport comment，只证明 RepoPilot 与客户端连接存活。两者不可互相替代。
- Provider ticker 与节点 checkpoint 可并发写事件；同任务 sequence 在一个 RepoPilot 进程
  内以锁和有限唯一键重试保持连续。该语义不延伸到多进程/多副本 pub/sub。
- Checkpoint 仍是已提交节点/轮次恢复。Provider 生命周期只是诊断时间线；崩溃可能留下无
  terminal event 的旧 `call_id`，恢复会发起新调用，不宣称 in-flight replay 或 exactly-once。
- 同任务 resume/cancel 的检查与 runner 发布由任务级进程内锁串行化；并发 resume 只能有一个
  成功占用执行权。该保证不扩展为跨进程分布式租约。
- 半开熔断探针被取消时释放单探针许可但不伪造成功；若在 retry backoff 期间取消，由 resilient
  wrapper 写入 `cancelled` terminal 事件并继续传播 `CancelledError`。
- 显式文件摄取与目录扫描共享同一 allowlist，拒绝符号链接、VCS/凭证目录、credential-like
  文件、私钥格式和不支持的后缀；BM25/哈希语义融合的 Top-K 先覆盖不同来源再按分数回填。
- 发布校验器不信任 manifest 自证：它独立打开 wheel/sdist，要求各一个，拒绝路径穿越、链接、
  禁止文件名和高置信密钥模式，并核对 wheel `METADATA` 与 sdist `PKG-INFO` 的 Name/Version。
- Docker 镜像中的应用和虚拟环境由 root 持有，运行用户保持非 root，只有 `/app/data` 可写；
  Compose 进一步启用只读根文件系统、`/tmp` tmpfs、丢弃全部 capabilities 和
  `no-new-privileges`。基础镜像 tag 同时固定到实际解析的 digest。

### v1.3 指标与真实 API 口径

deterministic 评测用于发现工作流、检索、引用和拒答回归，不代表真实模型质量或线上吞吐。
真实 API 完整流程只证明指定日期、模型和配置下的接口兼容、生命周期、状态机、fallback
传播、证据与引用契约。除非另有固定带标签数据集、并发模型、预热策略和负载方法，不得把
单次 TTFT、总耗时、`completed` 或 `degraded=false` 写成质量、容量或生产 SLO。

2026-07-22 最终真实 Provider 验收使用配置模型 `grok-4.5`（实际响应模型
`grok-4.5-build-free`）、`max_steps=1`、临时 SQLite 和单文件窄目标。Planner、Researcher、
Reviewer、Writer 各完成 1 次上游 SSE 调用；每次均观察到 `started → first_byte → completed`，
TTFT 为约 1.18–1.48s，四次 `fallback_used=false`。任务以 `completed`、`degraded=false` 结束，
1 条 accepted evidence、最终引用、`final` checkpoint 与 URL/Key 不落库检查全部通过。另两个
覆盖要求更宽的目标在 Reviewer 仍要求补证时分别诚实进入 `guarded`，未被包装成普通成功。

### v1.3 deterministic 评测产物

- report：`evals/report.json`
- schema：`1.1`
- dataset fingerprint：`d151f0b1db9161797313733e9bf255e44163a5d9bec425caac23d128e8cc24dc`
- report SHA-256：`3f9031e005b5d2896a7d91122ecb076e1cbdf39fecbedbe4891fdd9727da7bcb`

### v1.3 发布产物与容器

最终共享树在两个独立临时目录连续构建，wheel/sdist 逐字节一致；两个目录均通过
`scripts/check_release.py` 的 archive、metadata、危险成员、secret pattern、manifest 和
checksum 独立校验。clean venv 安装 wheel 后，CLI 与安装版本 `1.3.0` smoke 通过。

| 产物 | 字节 | SHA-256 |
| --- | ---: | --- |
| `repo_pilot-1.3.0-py3-none-any.whl` | 84,031 | `a65e26cf1c219818d9117c7f1c61c0324644ec0d97a3db787c4de1d0b6f2ccb5` |
| `repo_pilot-1.3.0.tar.gz` | 229,870 | `0b347fb8ed33fe2f1f3077494a2e4235dd907fccda212c3823e29657b8a11c56` |

最终本地镜像 `repopilot:1.3.0-final` 的 Image ID/RepoDigest 为
`sha256:9ffd8e5a795d3948b98c4d18fb13b7b0847352ea9d4416eec2490ff03413e167`。
容器 `/ready`、非 root、read-only rootfs、`cap_drop: ALL`、`no-new-privileges`、`/tmp`
tmpfs 和仅 `/app/data` 可写全部实测通过；测试容器、网络、卷与本地 smoke tag 已清理。

## v1.2.0 验收状态

以下为历史冻结记录，不因 v1.3 修改而重算或改写。

v1.2 已将默认控制平面迁移为真实 LangGraph `StateGraph`，显式表达
Planner → Researcher ⇄ Reviewer → Writer；角色内部仍保留模型驱动工具循环，证据硬门槛、
节点/轮次恢复、SSE `Last-Event-ID` 与 deterministic baseline 契约不变。源码、评测、浏览器、
wheel/sdist、Docker/Compose、恢复、持久化和降级路径均已按本节实测冻结，v1.2.0 在单用户、
自托管、只读仓库研究的产品范围内完成发布验收。

| 检查 | v1.2 实际结果 |
| --- | --- |
| 默认编排 | 真实编译 `StateGraph`；Planner → Researcher ⇄ Reviewer → Writer |
| 测试 | 108 passed；Ruff、format、strict Mypy 全部通过 |
| 分支覆盖率 | 86.35%，超过 85% 门槛 |
| 固定离线评测 | 30 cases；task success 0.9667；Recall@5 0.9583；citation validity/precision 1.0；refusal accuracy 1.0；degraded/fallback 0；本机重复运行 P95 约 0.35–0.40s |
| 评测可信性 | schema 1.1；完整路径 exact/目录 prefix 标签；每 case 记录 Top-5；两次顺序复跑非延迟指标一致 |
| 前端 | Playwright 1440×900 与 390×844：真实摄取/任务/SSE 成功，无 console error/warning 或横向溢出 |
| 发布包与容器 | 2 个 v1.2.0 发行包校验通过；Docker 非 root；API/SSE/metrics/resume/degraded/重启持久化和 Compose 通过 |

### v1.2 已验证行为

- Graph inspection 能看到四个命名节点和 Reviewer 条件返工边。
- Researcher 的模型 → 工具 → observation 循环仍受只读 allowlist 和全部预算约束。
- Checkpoint 恢复的是已提交节点/轮次状态，不宣称重放在途 Provider HTTP 请求。
- SSE 继续从 SQLite 事件表短轮询；`Last-Event-ID` 只补发已提交且序号更大的事件。
- SQLAlchemy `TaskStore` 是任务、事件与 WorkflowState Checkpoint 的单一持久恢复真源；
  LangGraph 只负责执行图，不使用第二套 saver，避免双写与恢复状态分叉。
- Reviewer 提交后若语料在 Writer 入场前漂移，Writer 清空旧 Evidence、标记
  `corpus_drift`、跳过模型调用并安全拒答；不新增可能在持续摄取下失活的 Writer 回边。
- expected source 不再用 basename 子串判断；`tests/test_agentic_workflow.py` 不能冒充
  `src/repopilot/workflow.py` 命中。当前唯一 Recall miss 是 `workflow-refusal`，其 Top-5
  由测试/设计文档占据，未通过修改标签或隐藏结果抹平。
- 30-case 评测仍明确 `claim_support_evaluated=false`、
  `semantic_review_evaluated=false`；相应 rate/P/R/F1 保持 `null`。

### v1.2 发布产物

| 产物 | 字节 | SHA-256 |
| --- | ---: | --- |
| `repo_pilot-1.2.0-py3-none-any.whl` | 67,844 | `74a29d1e000a19b5ce4371f18ae9f077014f0a01eb95475754ae066351070e4a` |
| `repo_pilot-1.2.0.tar.gz` | 194,048 | `f51385a97874b80ea685f753ce04d06acc2670a08ab042d6577106f26d671d18` |

`scripts/check_release.py` 验证 manifest、文件大小、SHA-256、发行包集合和外部
`SHA256SUMS` 一致；版本从 `pyproject.toml` 单一读取，不再在 release 脚本中写死。使用两个
独立临时构建目录连续构建，wheel 与 sdist 均逐字节一致。生成型 eval report 和本验收记录
不进入 sdist，避免时间戳、P95 或事后填写的哈希反向改变被记录产物；dataset 与 historical
baseline 仍保留在源码包中。

最终本地镜像为 `repopilot:1.2.0`，以 `repopilot` 用户运行，Image ID/本地 RepoDigest 为
`sha256:ececd1ca57c1bcbbd8bbc486d1e5c83f96437bed903c55ee9f91d700c5b0f866`，inspect 大小
78,889,107 bytes。容器实测摄取 63 个文档，任务 `completed` 且 `degraded=false`，5 条
Evidence 全部 accepted，11 个持久事件，`Last-Event-ID` SSE 回放、metrics 和删除/重建
容器后的 SQLite 数据均通过。独立任务还实测 cancel → resume → completed（14 个事件、
非 degraded），空工作区实测 `degraded=true` 并明确拒答。唯一命名的 Compose smoke project
验证只读 `/workspace`、可写命名卷、`/ready` 与无嵌入 secret，测试后已清理其容器、网络和卷。

该 digest 是本地构建证明，不是远程 Registry 推送证明；deterministic 评测也不是实时模型
质量、吞吐或容量结论。

## v1.1.0 验收状态

日期：2026-07-21 · 范围：模型驱动、证据优先、受 Harness 约束的有界多角色研究 Agent

v1.1 的最终验收以 `RELEASE_GATES.md` 为准。下表必须由本次最终门禁的真实输出填写；
在命令尚未完成前，不沿用 v1.0 的测试数、覆盖率、延迟、包摘要或镜像摘要冒充 v1.1
结果。

| 检查 | 命令或方式 | v1.1 结果 |
| --- | --- | --- |
| 单元/集成/API 测试 | `uv run pytest` | 103 passed |
| 静态检查 | `uv run ruff check .` | 通过 |
| 格式 | `uv run ruff format --check .` | 49 files already formatted |
| 严格类型 | `uv run mypy src` | 30 个源文件通过 |
| 分支覆盖率 | `uv run pytest --cov=repopilot --cov-branch` | 86.36%，门槛 85% |
| 锁文件/依赖 | `uv lock --check --offline` + `uv pip check` | 49 个锁定包；48 个已安装包兼容 |
| 固定离线评测 | `evals/v1.1-report.json` | 30 cases；task success 0.9667；Recall@5 0.9583；citation validity/refusal accuracy 1.0；0 degraded/fallback；P95 144.691ms |
| 前端语法/浏览器 | Node syntax + Playwright desktop/mobile | 1440×900 与 390×844 均无控制台错误、警告或横向溢出；状态刷新后保持 |
| Python 发布包 | `python scripts/build_release.py --out-dir release` + `python scripts/check_release.py release` | wheel/sdist 共 2 个，外部 manifest/checksum 验证通过，包内无自引用记录 |
| Docker/Compose | build + `/ready` + API/SSE/metrics/持久化冒烟 | 非 root、健康检查、摄取、任务、证据、SSE、metrics、cancel/resume、命名卷持久化和 Compose 均通过 |

### v1.1 必验行为

- Planner JSON 计划经过本地 Schema/数量/长度校验，并实际驱动检索。
- Researcher 的模型工具调用只能进入注册的只读仓库工具；未知工具失败关闭。
- Reviewer 先过代码硬门槛，再由模型做相关性/蕴含缩小；模型不能提升硬拒绝证据。
- Reviewer 证据缺口最多触发配置允许的额外返工轮数。
- Writer 只收到 accepted evidence；无引用或越界引用触发 evidence-only 降级。
- Provider fallback provenance 必须传播为任务 `degraded=true`。
- WorkflowState 从节点/轮次恢复，Evidence 在返工和恢复后仍保持幂等最终快照。
- 仓库内容中的提示注入不能扩大工具白名单、预算或系统权限。

### v1.1 指标口径

发布报告至少区分 retrieval recall、citation validity、claim support、semantic review、
revision success/limit、fallback/degraded rate、task success 和 P95 latency。逐主张支持率
必须按 claim/citation 检查，不能继续用 `1 - refusal_accuracy` 代替；semantic review 的
precision/recall/F1 与 adversarial decoy rejection 也只能来自带标签的 reviewer decisions。
没有相应标签时必须标记 `evaluated=false` 并将不可计算值写为 `null`。真实 Provider 若未跑
固定数据集和明确配置，只能报告“接口可用性测试”，不能写成模型质量或线上吞吐基准。

本次 v1.1 固定评测为 deterministic 离线回归，不是线上吞吐或真实模型质量基准。当前
数据集没有逐主张标注或人工 reviewer decision 标注，因此报告明确写入
`claim_support_evaluated=false`、`claim_support_rate=null`、
`semantic_review_evaluated=false`，并将 semantic review precision/recall/F1 与
adversarial decoy rejection rate 写为 `null`，不使用代理指标冒充人工标注评测。
本次 deterministic 数据集也没有触发 reviewer revision request，因此 revision request、
execution 和 limit 计数均为 0，`revision_success_evaluated=false` 且 success rate 为 `null`；
这不应解释为 100% 返工成功率。

评测生成于 `2026-07-21T10:00:17.467150Z`。数据集 SHA-256 为
`12a36f41888ae9b12116a5da95dede81711c6bfe4e9ac39bf143b79b89754a90`，语料包含
62 个文档，语料指纹为
`983eee7103cc036740b9fdcc0494e7da152bfcaeef1369ee9de00f0942842ca7`。唯一未命中用例是
`provider-circuit`：报告引用均可解析且关键词检查通过，但前 5 个引用未包含期望的
`resilient.py`。该结果仍高于 CI 的 task success 0.9 与 Recall@5 0.85 门槛，未通过修改
冻结用例或伪造结果来抹平语料排序漂移。报告文件 SHA-256 为
`db00a9b0e0ac0704d7210a5ae24b6b43bd2724d865a008da3b8bd888905ae3ce`。

### 真实 Provider 接口验证

真实 OpenAI-compatible 端点只用于接口与降级链路验证，没有跑固定数据集，因此不报告为
模型质量、延迟或吞吐基准。端点地址与 Key 未写入仓库、日志、报告或产物；配置模型为
`grok-4.5`，实际响应模型为 `grok-4.5-build-free`。

- 首次四角色验证中 Planner、Researcher、Reviewer、Writer 各有 1 次主 Provider 成功，
  无 fallback；1 条 accepted evidence 可解析，Writer 生成 2 个合法引用。该验证使用
  `max_review_rounds=0`，Reviewer 请求返工后按预期以 `review_limit_reached` 标记 degraded。
- 正式配置重跑使用 `max_steps=1`、`max_review_rounds=2`、临时 SQLite 和 61 文档语料。
  健康检查可用；Planner、Researcher 各有 1 次主 Provider 成功，Reviewer、Writer 因远端
  响应超时各进入 1 次显式 fallback。任务仍完成并正确标记 `degraded=true`，原因为
  `reviewer_fallback`、`writer_validation_failed`；17 条 accepted evidence 的引用均可解析。
- 收紧 Reviewer 字段类型与 Writer 引用模板后，以正式 `max_steps=1`、
  `max_review_rounds=2` 再次运行。Planner、Researcher、Reviewer、Writer 各有 1 次主
  Provider 成功，零 fallback；Reviewer 接受 1 条证据且无需返工，Writer 生成 2 个合法
  `[1]` 引用。最终任务 `status=completed`、`degraded=false`、降级原因为空，1 条 accepted
  evidence 的引用可解析；实际响应模型为 `grok-4.5-build-free`。

因此本次发布既证明正式配置下四角色协议可由真实 Provider 全主模型、零降级执行，也证明
端点不可用时的超时 fallback provenance 能完整传播。上述运行只属于接口与状态机验收，
不属于固定数据集模型质量、延迟或线上吞吐基准。

### v1.1 发布产物

最终发布产物位于被 Git 忽略的 `release/`，外部记录不进入 wheel/sdist。最终验收记录
`docs/ACCEPTANCE.md` 也从 sdist 排除，避免记录包哈希时反向改变被记录的包。

| 产物 | 字节 | SHA-256 |
| --- | ---: | --- |
| `repo_pilot-1.1.0-py3-none-any.whl` | 64,409 | `7cd7ec028c88056272765ebfcd5a67f02013c2b46691edd8e68754ce569f1dcf` |
| `repo_pilot-1.1.0.tar.gz` | 160,013 | `4fa56f0485de8416ef9e91f1cbf3fc1e8caa67cae0708d4be43646bcd68b7c2d` |

`python scripts/check_release.py release` 验证 2 个产物通过；包内不存在
`docs/ACCEPTANCE.md`、`release-manifest.json`、`SHA256SUMS` 或 `release/` 目录。

最终 Docker 标签为 `repopilot:1.1.0`，以 `repopilot` 用户运行。完整冒烟观测到 `/ready`
成功、README 摄取 1 个文档/3 个 chunks、任务完成且非 degraded、3 条 evidence（其中 1 条
accepted）、7 个带 ID 的 SSE 事件、Prometheus 创建/完成计数、cancel/resume 事件，以及
删除并重建容器后的 SQLite 命名卷持久化。Compose 容器使用只读 `/workspace`、可写命名卷
`/app/data`，未嵌入 secret。最终 Image ID/本地 RepoDigest 为
`sha256:f0686778d017f79c995432a675acc4402424911379dae4ad23bd80a4ebc39e91`，inspect 大小
66,841,304 bytes；该本地摘要不是远程 Registry 推送证明。

---

## v1.0.0 历史冻结验收

日期：2026-07-20 · 环境：WSL2/Linux · Python 3.12 · uv · Docker · Playwright

以下数字只属于 v1.0.0 历史版本，不会被 v1.1 改写。

| 检查 | 历史结果 |
| --- | --- |
| 单元/集成/API 测试 | 76 passed |
| Ruff / format / strict Mypy | 全部通过 |
| 分支覆盖率 | 85.58%（门槛 85%） |
| 固定评测 | 30 用例全部通过；精确数据见 `evals/baseline.json` |
| Python 发布包 | wheel/sdist 构建与内容检查通过 |
| Docker/容器冒烟 | 非 root；health/ready/摄取/任务/证据/SSE/metrics 通过 |
| 浏览器冒烟 | Playwright 1440×900 与 375×812，零控制台错误 |

历史离线评测记录 task success rate、Recall@5、citation precision、refusal accuracy 均为
1.0，`unsupported_claim_rate` 为 0.0，P95 低于当时 200ms 门槛。这里的 citation
precision 表示引用可解析率；`unsupported_claim_rate` 由拒答准确率派生，并不是逐主张
事实支持率。语料文档数和 SHA-256 指纹记录在历史 baseline 中，用于区分代码回归与语料
变化。

### v1.0 已知限制（历史）

- Planner 是词法计划，Reviewer 只做一次分数/覆盖率筛选，只有 Writer 使用真实模型。
- ResearchWorkflow 忽略恢复消息并从头重放，只因当时节点均只读而可接受。
- 哈希嵌入只提供确定性弱加成，不是学习型 embedding。
- SSE 使用 200ms 数据库轮询和单副本语义。
- 未执行提示注入专项对抗评测，也没有逐主张 groundedness 指标。
- 没有使用历史聊天中暴露的 API Key；真实 Provider 必须使用已轮换密钥。
