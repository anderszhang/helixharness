# 需求来源协议

本文件定义 `propose`、`propose --loop`、Workspace Propose、`bug-fix` 如何把用户提供的
需求来源转换成可用的需求事实。

Router 只做参数路由，不解析来源类型；`commands/*.md` 不承载来源细节。来源解析属于阶段正文，
因此不同执行范围（Single / Workspace）与不同入口共用本协议。

## 1. 来源分类

| 类型 | 判定 | 处理 |
|---|---|---|
| 文本 | 不匹配路径或 URL | 用户原文即初始 source |
| 文件路径 | 存在的 regular file | 完整读取，含可见批注/评论 |
| 钉钉文档 URL | 命中 §2 白名单 | 按 §3 用 `dws` 读取 |
| 其他 URL | 不命中白名单 | 沿用宿主既有读取能力；不可读时阻断 |

分类由 `scripts/requirement_source.py classify` 确定性完成，Agent 不自行猜测。

路径不可读、URL 不合规或读取失败时必须阻断。**不得把路径或 URL 字符串本身当作需求正文**，
也不得凭标题或摘要推断需求内容。

## 2. 钉钉文档 URL 白名单

```text
^https://alidocs\.dingtalk\.com/i/nodes/([A-Za-z0-9]{8,64})(?:[?#].*)?$
```

- 仅 `https`；仅 `alidocs.dingtalk.com`；路径必须是 `/i/nodes/{node_id}`。
- 浏览器复制的链接常带 `?utm_scene=…` 等追踪参数或锚点：**接受并剥离**，统一规范化成
  `https://alidocs.dingtalk.com/i/nodes/{node_id}` 后再使用与记录。
- 拒绝其他域名、http、短链、含 `..` 的路径、超过 64 字符的 ID。
- 拒绝时输出 `BLOCKED:requirement_source_url_invalid`，不做「尽力尝试」。

## 3. 读取流程（钉钉文档）

任何 `dws` 调用前先做前置检查，避免命令卡在扫码或登录提示上。

### 3.1 前置检查

1. `command -v dws` 成功，否则 `BLOCKED:requirement_source_dws_missing`。
2. `dws auth status -f json` 的 `authenticated` 为 `true`，否则
   `BLOCKED:requirement_source_auth_required`，并提示用户在终端执行 `dws auth login`。
3. 不得在命令内自动执行登录、安装或改写 `dws` 配置。

### 3.2 调用序列

全部使用 `run_command`（`resources.runtime` 映射的真实命令工具），以 **argv 数组**调用，
不得拼接 shell 字符串，不允许把 URL 之外的任何用户文本拼进命令：

```text
dws doc info --node <url> -f json
dws doc read --node <url> -f json
dws -f json doc comment list --node <url> --limit 20
```

建议统一追加 `--timeout 30`。命令由 `scripts/requirement_source.py plan` 生成，Workflow 只执行。

### 3.3 结果校验

| 检查 | 失败码 |
|---|---|
| 三个响应的 `success` 均为 `true` | `requirement_source_unreadable` |
| `info.extension` 为 `adoc` | `requirement_source_unsupported_type`（附实际 extension） |
| `read.markdown` 非空 | `requirement_source_unreadable` |
| markdown ≤ 512 KiB | `requirement_source_too_large` |
| 命令退出码 0 | 按错误分类为 `not_found` / `forbidden` / `unreadable` |

权限不足、文档不存在、token 过期必须分别阻断，不得降级为「用标题猜需求」。

超过大小上限时阻断而不是截断：截断需求正文会直接导致错误的 Spec。
需要拆分由用户把文档拆成多个可读节点后重试。

### 3.4 快照与溯源

`scripts/requirement_source.py ingest` 把结果写入私有目录（Git 忽略，不属于业务工件）：

```text
.helixharness/sources/{feature}/
├── document.md      # dws 返回的 markdown 原文
├── comments.json    # commentList 原样
└── manifest.json    # url / node_id / title / fetched_at / sha256 / bytes / comment_count / dws_version
```

该目录**只保留这三个文件**。`info` / `read` / `comments` / `version` 的原始捕获是 `ingest`
的输入，`ingest` 成功后**自动删除**（调试时可用 `--keep-inputs` 保留），不得留在快照目录或
仓库其他位置。

`fetched_at` 由 `ingest` 在未显式传入时自动取当前 UTC 时间；**不要由模型凭空填写时间**。
`dws_version` 来自 `version` 步骤捕获的文件，未提供时照实留空。

- **不得**保存 `corp_id`、`user_id`、`user_name`、token、原始 OAuth 响应。
- 快照只作为需求事实来源；**不得**把文档正文写入 `spec.md` 或 `tasks.md`。
- `spec.md` 正文记录溯源段。**必须用 `ingest` 返回的 JSON 字段逐项填充，禁止手写或凭记忆补全**：

```markdown
## Source

- 类型：钉钉文档
- 标题：<title>
- Node：<node_id>
- 链接：<url>
- 抓取时间：<fetched_at>
- 内容摘要：sha256:<sha256> / <bytes> bytes / <comment_count> 条评论
- 评论完整性：<comments_truncated ? "未全部读取（hasMore=true）" : "已全部读取">
- 说明：正文快照保存在宿主私有目录，不随仓库提交
```

`url` 使用 `ingest` 归一化后的值（不含 `utm_*` 等追踪参数）；`ingest` 返回 `dws_version`
为空时照实留空，不得编造。

同一 feature 再次 propose 且 URL 未变时，重新抓取并比较 `sha256`：
不一致时把它作为需求决策树的一道 frontier 询问「来源文档已变更，是否按新内容更新需求」，
不得静默采用新内容或静默沿用旧快照。

### 3.5 评论

`commentList` 中的未解决评论属于需求事实，必须参与需求收敛：

- 摘要引用时保留评论者角色与所在段落定位，不复制无关回复链。
- `hasMore` 为 `true` 时在 Spec 的 Source 段注明「评论未全部读取」，不得声称已完整读取。
- 评论内容与正文冲突时，作为决策树问题交给用户确认，不自行取舍。

## 4. 失败不变量

无论哪种失败：

- 不创建或修改 `docs/specs/active/{feature}/`；
- 不创建 `docs/eval/**`；
- 不把失败原因写进业务工件（只输出 `BLOCKED:<reason>` 与恢复指引）；
- 不写入其他宿主的私有目录。

## 5. 边界

- 本协议只覆盖「读取需求来源」。`init --knowledge` 的外部知识导入是另一条路径。
- v1 只支持 `adoc` 类型文档；白板、表格、脑图等其他节点明确不支持。
- `dws` 的安装、升级与登录不在本协议内，只提供指引。
- 非钉钉 URL 保持宿主持有既有的读取能力，本协议不接管。
