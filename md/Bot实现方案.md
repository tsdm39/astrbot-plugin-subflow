# 字幕组任务管理 Bot 实现方案

> 配套设计文档：[字幕组Bot设计文档.md](字幕组Bot设计文档.md)

## Context

零代码起步的 NoneBot2 插件，把字幕组按"集"推进的工作流（接活/完成/依赖跟踪/通知）从手工填表的腾讯文档智能表上自动化起来。本方案做两件事：

1. 记录已验证的腾讯文档智能表 Open API 用法（spike 已跑通）
2. 固化所有在 grilling 阶段敲定的设计决策，并指出对原设计文档的修订点

---

## 一、Spike 已验证的 API 事实（基于实跑）

Spike 代码：[../spike/tencent_spike.py](../spike/tencent_spike.py)（已通过 add→update→delete 完整 cycle，全部 ret=0）

- **认证**：三头部 `Access-Token` / `Client-Id` / `Open-Id` + `Content-Type: application/json`
- **统一端点**：`POST https://docs.qq.com/openapi/smartbook/v2/files/{fileID}/sheets/{sheetID}`，body 顶层 verb 区分操作（`getRecords` / `addRecords` / `updateRecords` / `deleteRecords` / `getFields`）
- **fileID 转换接口**：URL 里的 encodedID（如 `DUmRFZmFwcnNCcEZv`）不能直接当 fileID 用，必须先调 `GET /openapi/drive/v2/util/converter?type=2&value=<encodedID>` 换成 `300000000$XXX` 形式。Bot 启动时缓存映射。
- **字段值格式（关键坑）**：
  - 文本（type 1）：`[{"type":"text","text":"值"}]`
  - 单选（type **17**，文档错写 9）：写入 `[{"text":"选项名"}]`，读出 `[{"id":"...","style":...,"text":"..."}]`。**单选字段送裸字符串会被静默丢弃**（ret=0 但值变空数组）。
  - 日期/时间（type 4）：unix 毫秒字符串，如 `"1776768420000"`
- **Rate limit**：converter 接口 300/period openID，业务接口未明示，按个人开发者 2000 次/天总额度。

---

## 二、设计决策（grilling 共 11 项，覆盖对原设计文档的修订）

### D1 · 表结构改造（必须先做，由用户在腾讯文档完成）
原设计文档 2.3 节列出 7 列，但用户的测试表只有合并的"项目"列。决定：**用户在腾讯文档手工给每个番剧子表新增两列**：
- `集数`（文本类型）
- `分段`（文本类型）

可选保留或删除原"项目"列；Bot **完全不读** `项目` / `开始时间` / `相关流程` 三列（让人类自由填）。

### D2 · Token 续期：手动模式
开放平台后台直接签发的 access_token（30 天有效），无 client_secret 也无 refresh_token。Bot **不自动刷新**：
- 启动时读 `.env` 里的 `TENCENT_DOC_ACCESS_TOKEN`，解码 JWT 看 `exp`
- 距过期 ≤ 7 天：日志 warning + 启动时在总群发提醒
- 已过期：拒绝启动，提示用户去后台重新生成 token
- 运营约定：用户每 25 天手动续一次

### D3 · `/完成` 权限：全员可完成 + 提醒
覆盖原设计 4.2 节的"只有当前组员才能完成"：**任何工作群成员都可执行 `/完成`**。但：
- 发送者 QQ == 组员 字段：正常完成提示
- 发送者 ≠ 组员（QQ 形式）：附 `⚠️ 你不是该任务的当前组员（@原组员），已为你标记完成`
- 发送者 ≠ 组员（昵称形式）：附 `⚠️ 此任务原本由「<昵称>」承担`

`/放弃` 沿用同一规则。

### D4 · 管理员定义：并集 + 超管分层
- **群内管理员命令**（`/绑定` `/解绑` `/新建集` `/新建特殊` `/删除任务` `/修改任务` `/归档` `/设置流水线`）：当前群的 群主/群管 **∪** `SUBFLOW_ADMIN_QQ_LIST`
- **超级管理员命令**（如 `/绑定列表 全部`）：**仅** `SUBFLOW_ADMIN_QQ_LIST`
- 实现：NoneBot2 自带 `GROUP_OWNER | GROUP_ADMIN` permission + 自定义读 list 的 permission，OR 组合

### D5 · 并发：进程内 asyncio.Lock 串行化每条 record
`task_manager.py` 维护 `dict[(file_id, sheet_id, record_id), asyncio.Lock]`，所有写操作（接活/完成/放弃/进行中/修改）`async with lock:` 包住"读缓存→调 API→重读→更新缓存"四步。后到者拿"已被 @xxx 接走"清晰错误。多实例部署需换 Redis 锁（暂不实现）。

### D6 · 集数/分段宽容匹配
- **存储**：Bot 写入用首次输入的原样（`集数="07"`、`分段="P1（0-8）"` 全角括号）
- **匹配**（命令解析时归一化）：
  - 集数：去前缀「第」、去后缀「集」、去前导零（全 0 保留）、小写化
  - 分段：去全/半角括号内容，保留 `P+数字`；`全集` 固定字符串
- 实现：`task_manager.py` 暴露 `normalize_episode()` / `normalize_segment()`

### D7 · `/删除任务` 二次确认：内存 dict + 懒过期
- 数据结构：`pending_confirmations: dict[(group_id, user_id), {action, expires_at, payload}]`
- Key 含群号+用户 QQ：A 用户的确认不会被 B 接管；不同群互不干扰
- 同一 key 后发 `/删除任务` 自动覆盖前发，提示"已有未完成确认被覆盖"
- 用户回 `确认删除` 时检查 `expires_at`，过期则提示重新发起
- 重启清空（可接受）
- 超时可配置 `SUBFLOW_CONFIRM_TIMEOUT=30`

### D8 · 缓存一致性：写后重读
- 写操作 API 成功后，**立即调一次 `getRecords` 拉刚写的 recordID** 作为校验，并用拉到的数据更新本地缓存（不仅是本地拼凑）
- 写失败/超时：不更新缓存，回复"操作失败，请重试或等下次同步"
- 30 分钟全量同步保持作为兜底
- 实现：在 `storage/tencent_doc.py` 的写方法返回最新 record；`cache.py` 的 `update_local()` 用返回值覆盖
- 待确认：腾讯 `getRecords` 是否支持按 recordID filter；不支持就拉一页+本地 find

### D9 · 绑定模型边缘
- **总群禁用 `/绑定` `/解绑`**：总群不持有绑定关系；提示去工作群操作
- **一群多番时省略番剧名**：单绑定可省，多绑定回 `本群绑定了 [淡岛百景|孤独摇滚]，请指明`
- **`/绑定id` 别名全局唯一**：冲突拒绑并提示已存在

### D10 · 流水线快照（per 集）
落实原设计文档 3.3 节"改流水线只影响新建集"的承诺：
- **新增数据文件**：`data/episode_pipelines.json`，结构 `{番剧: {集数: [stage1, stage2, ...]}}`
- `/新建集` / `/新建特殊` 执行时把**当前番剧流水线深拷贝**存入对应 key
- `/完成` 的依赖检查 **优先** 读快照；快照不存在（手动加的集）则 fallback 读当前流水线 + warning 日志
- `/设置流水线` 修改的是当前番剧默认流水线，不影响已快照集

### D11 · 单选字段写入用 `[{"text":"..."}]` 形式
不可用裸字符串。`storage/tencent_doc.py` 的写方法对单选字段（类型、进度）做包装。具体哪些列是单选由 `getFields` 启动时缓存的 schema 决定，避免硬编码。

---

## 三、项目结构（增量于原设计文档 5.2）

在原设计目录基础上，**增量**：

```
nonebot-plugin-subflow/
├── spike/                          # 已存在，feasibility 验证
│   ├── tencent_spike.py
│   ├── .env.spike                  # gitignored
│   └── last_response.json          # gitignored
└── data/
    └── episode_pipelines.json      # 新增（D10）
```

并在原设计的 `nonebot_plugin_subflow/` 下：
- `storage/tencent_doc.py` 内增 `_convert_encoded_id()`、`_wrap_field_value()`（处理单选/日期格式）、`_unwrap_field_value()`（读响应归一化）
- `task_manager.py` 内增 `normalize_episode()` / `normalize_segment()` / `pending_confirmations` 字典 / per-record `asyncio.Lock` 管理器
- `pipeline.py` 内增 `snapshot_for_episode()` / `get_episode_pipeline()`

---

## 四、关键 `.env` 字段（增量于原设计 5.3）

新增/调整：
```env
# Token 模式：手动续期（D2）
TENCENT_DOC_ACCESS_TOKEN=         # 30 天 JWT，到期前手动更新
TENCENT_DOC_OPEN_ID=              # 与 client_id 同时签发
TENCENT_DOC_CLIENT_ID=
# 原 TENCENT_DOC_CLIENT_SECRET / TENCENT_DOC_REDIRECT_URI 暂不需要

# 行为开关
SUBFLOW_CONFIRM_TIMEOUT=30        # /删除任务 确认窗口秒数（D7）
SUBFLOW_TOKEN_WARN_DAYS=7         # token 距过期多少天开始提醒（D2）
```

---

## 五、实现里程碑（建议顺序）

1. **M1 — Storage 层完整可用**
   - `storage/base.py` 抽象接口
   - `storage/tencent_doc.py`：认证、converter、getFields（启动时缓存字段类型）、getRecords（含按 recordID 拉单条）、addRecords、updateRecords、deleteRecords、字段值包装/解包
   - 单元测试用 spike 跑过的 `.env.spike` 配置回归

2. **M2 — Cache + 启动同步**
   - `cache.py` 全量拉取（包括 fileID 映射）+ 30 分钟定时同步 + 写后单条重读
   - 启动时 token JWT 解码 + 过期检查（D2）

3. **M3 — Bindings + Pipeline + 业务核心**
   - `bindings.py`（含 D9 的总群禁用、别名唯一、一群多番省略推断）
   - `pipeline.py`（DSL 解析 + D10 快照）
   - `task_manager.py`（D5 锁、D6 归一化、D7 确认状态、D8 写后重读、依赖检查）

4. **M4 — Commands 层**
   - 所有命令处理器接入 NoneBot2
   - 权限装饰（D4 union 模型）
   - 消息渲染（D3 提醒文案、`/进度` 排版）

5. **M5 — 部署**
   - Dockerfile + docker-compose.yml（按原设计 5.4）
   - README

---

## 六、需要用户配合的事

| # | 事项 | 时点 |
|---|---|---|
| 1 | 在腾讯文档测试表新增"集数"和"分段"两列（文本类型）（D1） | M1 之前 |
| 2 | 准备一个 QQ 测试群 + 一个测试号，启用 NapCat 接入 NoneBot2 | M4 调试时 |
| 3 | 给 Bot 配置 `SUBFLOW_ADMIN_QQ_LIST` 及总群 ID | M4 之前 |
| 4 | 后续每 25 天手动到腾讯文档开放平台后台刷新 access_token | 长期运营 |

---

## 七、验证方法

| 阶段 | 怎么验证 |
|---|---|
| M1 完成 | spike 风格的脚本，对每个 storage 方法跑 add/update/delete 实测，确认 ret=0 且数据落库 |
| M2 完成 | Bot 启动 → 检查日志显示"已加载 N 条记录、X 个映射"；让用户在腾讯文档手动改一条 → 等 30 分钟后查 `/进度` 看到变更 |
| M3 完成 | 单元测试覆盖：依赖检查（含 D10 快照）、归一化（含 D6 全/半角）、并发锁（asyncio.gather 两个 claim 同任务）、确认状态（D7） |
| M4 完成 | 在 QQ 群里跑设计文档 6 节"典型使用流程"，从 `/绑定` 到 `/归档` 全链路，对照预期输出 |
| 端到端 | 真实使用一周，记录漏接/重复接发生次数、token 过期提醒是否准时触发、缓存漂移日志条数 |

---

## 八、原设计文档需要回写的修订点

为避免设计文档与实现脱节，建议在 [字幕组Bot设计文档.md](字幕组Bot设计文档.md) 落 4 处修订（同步交付物）：
- 2.3 表头字段：明确"组员"可空、补充 `开始时间`/`相关流程` 为人类手填、`完成时间` 是 datetime
- 3.3：补充"流水线变更时已有集走快照"的实现细节（D10）
- 4.2 `/完成`：把"只有当前组员才能完成"改为"全员可完成 + 提醒"（D3）
- 4.1 管理员命令：开头补一段"管理员 = 群主/群管 ∪ SUBFLOW_ADMIN_QQ_LIST"的定义（D4）
