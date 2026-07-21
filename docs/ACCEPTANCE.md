# RepoPilot 验收记录

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
