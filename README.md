# Action Inbox 本地同步与总待办 Agent

Hammerspoon 每次捕获文字后，会同时：

1. 追加写入 `~/Documents/ActionInbox/captures.jsonl`；
2. 请求本服务的 `POST /captures`；
3. 本服务写入 SQLite 待同步队列；
4. 配置完成后，通过本机已登录的 `lark-cli` 写入飞书多维表。

服务同时提供一条总待办 Agent 链路：

1. 分页读取目标飞书多维表的全部记录；
2. 调用 DeepSeek API 做 JSON 结构化意图识别；
3. 过滤参考信息和噪声，合并重复事项，判断优先级与明确截止日期；
4. 为每项待办生成本机可访问的正式结构“执行资料”，并将未完成事项同步到同一个 Base 的“今日总待办”输出表；
5. 生成飞书 Markdown 总待办预览并保存到 SQLite；
6. 在输出表写入基于内部信息生成的“AI参考回答”，供飞书智能体后续提醒和追问；
7. 生成待发送的 Markdown 预览；飞书智能体发送将在下一阶段接入。

## 飞书多维表准备

创建一份多维表，建一个表格并使用以下 4 列：

| 列名 | 类型 |
| --- | --- |
| 待办 | 多行文本或文本（主字段） |
| 日期 | 日期，开启时间 |
| 时间 | 文本，格式 `HH:mm` |
| 来源 | 文本 |

把飞书应用机器人添加为该多维表的协作者，并授予编辑权限。然后从多维表 URL 获取 `base_token` 与 `table_id`。

> 本机 `lark-cli` 当前可使用应用机器人身份。若机器人没有这份多维表的权限，服务会把记录保留在 SQLite 的待同步队列中，并返回错误原因。

## 启动

```bash
cd /Users/new/Documents/ChatGPT/workless/action-inbox-service
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
set -a && source .env && set +a
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8787 --reload
```

在另一个终端验证：

```bash
curl http://127.0.0.1:8787/health
```

## 配置飞书同步

在 `.env` 设置：

```bash
FEISHU_BASE_TOKEN=app_xxx
FEISHU_TABLE_ID=tblxxx
FEISHU_IDENTITY=bot
FEISHU_DIGEST_TABLE_ID=tblxxx
FEISHU_CONTENT_FIELD=待办
FEISHU_DATE_FIELD=日期
FEISHU_TIME_FIELD=时间
FEISHU_SOURCE_FIELD=来源

FEISHU_RECIPIENT_TYPE=user
FEISHU_RECIPIENT_ID=ou_xxx

DEEPSEEK_API_KEY=你的DeepSeek密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_MAX_TOKENS=4096
DIGEST_PROMPT_FILE=prompts/todo_digest_system.md

MOCK_DOCUMENT_DIR=data/mock_documents
MOCK_DOCUMENT_PUBLIC_URL=http://127.0.0.1:8787
AUTO_DIGEST_ENABLED=true
AUTO_DIGEST_POLL_SECONDS=5
AUTO_DIGEST_DEBOUNCE_SECONDS=30
EVENT_LISTENER_ENABLED=true
```

配置完成并重启服务后，执行以下请求补发所有失败或待同步记录：

```bash
curl -X POST http://127.0.0.1:8787/sync/pending
```

`FEISHU_BASE_TOKEN` 和 `FEISHU_TABLE_ID` 可以从目标多维表 URL 解析；也可以把 URL 发给 Codex 处理。不要把飞书应用密钥写入 Hammerspoon 配置或提交进 Git。

`FEISHU_DIGEST_TABLE_ID` 是输出表“今日总待办”的 table ID。服务以 `本地待办ID` 更新这张表：同一待办会更新原记录，避免每天重复新增。输出表为飞书 Agent 的提醒数据源，可按 `优先级` 和 `截止日期` 排序。本地完成状态继续保存在 SQLite 中，不再作为输出表字段展示。

输出视图按重要性从左到右排列：`待办简称`、`你需要做的`、`优先级`、`截止日期`、`AI参考回答`、`资料链接`。每条待办只保留一个最小可执行动作；生成日期、来源记录 ID、本地待办 ID 等核对字段放在后部。本地完成状态继续仅保存在 SQLite。

输出表还使用两列：

| 列名 | 内容 |
| --- | --- |
| AI参考回答 | DeepSeek 基于当前内部记录给出的简短参考结论；关键事实缺失时会明确标出“待确认”。 |
| 资料链接 | 指向本机生成的执行资料，包含事实依据、处理建议和待确认信息。 |

`资料链接` 默认形如 `http://127.0.0.1:8787/documents/todo_xxx`，仅能在运行服务的这台 Mac 上打开。飞书 Agent 若部署在云端，无法读取该地址；届时需要为服务增加经过鉴权的内网访问入口，或把执行资料同步到飞书文档。

对于已提供演示资料要求的事项，服务还会生成可点击的本机参考文档或表单链接，并嵌入 AI参考回答和汇总卡片。参考文档正文会标注“本机演示数据”，真实业务使用前必须以权限内资料复核。

## 编辑 AI 提示词

提示词的单一维护位置是：

[`prompts/todo_digest_system.md`](prompts/todo_digest_system.md)

这里定义了意图分类、合并规则、优先级、日期推断边界、风险提示和 JSON 输出结构。修改后重启服务即可生效，不需要改 Python 代码。

提示词中的 `{{CURRENT_DATE}}` 和 `{{TIMEZONE}}` 会在每次请求时被服务自动替换。不要删掉“json”字样和末尾 JSON 结构，DeepSeek 的 JSON 模式依赖它们；如果要加业务规则，建议加在“判断规则”中。

## 生成总待办

先生成预览：

```bash
curl -X POST http://127.0.0.1:8787/digest/preview
```

响应包含：

- `run_id`：本次不可变预览的 ID；
- `report`：结构化意图、优先级、截止日期、证据和风险；
- `message_markdown`：将要发送的完整消息。
- `mock_documents`：本次为每项待办生成的执行资料路径与本机链接。

`/digest/<run_id>/send` 会以当前飞书应用机器人的身份发送一张汇总卡片，默认仅展示优先级最高的 3 条待办。每条包含一个最小动作、简短参考回答和资料链接；卡片底部提供“打开待办总表”按钮。实际发送前仍需确认收件人、卡片内容和发送身份。

卡片的“打开待办总表”链接可在飞书客户端跨设备使用。资料链接是 `127.0.0.1` 本机地址，只能在运行服务的 Mac 上打开。

## 自动刷新与完成待办

服务每 5 秒读取一次原始待办表；检测到变化后等待 30 秒，重新分析并自动发送 1 张最新汇总卡。连续修改会重置 30 秒倒计时，避免连发卡片。

汇总卡片中每条待办都有“完成：事项”按钮。点击后会把完成状态保存到本机 SQLite，并在 30 秒后发送最新汇总卡。也可在与机器人的私聊中发送“完成 巴黎贝甜货盘”这类文本命令。

在飞书开放平台的“事件与回调”中启用 `im.message.receive_v1` 和 `card.action.trigger`，并确保应用拥有 `im:message.p2p_msg:readonly` 与 `im:message:readonly` 权限；否则服务可以运行，但无法收到私聊回复或卡片点击。

如果需要测试本机机器人发送，确认预览内容、收件人和发送身份后调用：

```bash
curl -X POST http://127.0.0.1:8787/digest/<run_id>/send \
  -H 'Content-Type: application/json' \
  -d '{
    "confirm_send": true,
    "confirm_recipient_id": "ou_xxx",
    "confirm_identity": "bot",
    "dry_run": false
  }'
```

联调阶段把 `dry_run` 改为 `true`。服务会让 `lark-cli` 只输出请求预览，不实际发送。

同一个 `run_id` 使用固定幂等键，已经成功发送的预览不会重复发送。每次发送都复用 SQLite 中保存的预览文本，模型不会在确认后再次改写内容。

## 意图与优先级规则

- 意图：`task`、`follow_up`、`schedule` 会进入总待办；`reference`、`ignore` 会被过滤。
- 去重：语义相同的多条记录合并，并保留全部飞书 `record_id` 作为证据链。
- 截止日期：只接受原文明确日期，或能基于当前日期可靠解析的相对日期。
- 优先级：`P0` 为已逾期或当天影响外部承诺，`P1` 为近期明确截止或阻塞他人，普通行动为 `P2`，低价值且无期限为 `P3`。
- 对抗性校验：程序拒绝模型生成不存在的 `record_id`、无来源待办、无效 ISO 日期，以及被错误放进总待办的参考信息。

## 本地完成状态

每个生成的待办会按来源 `record_id` 获得稳定的本地 `todo_id`，状态只保存在 SQLite，不回写原始多维表。标记完成后，对应来源记录会在下一次总待办分析前被排除。

输出表的 `状态` 是本地状态的同步副本。飞书 Agent 后续如需要提供“完成”按钮，应调用本地服务的状态接口，再由服务同步回输出表，避免出现两套状态相互覆盖。

查看本地待办：

```bash
curl http://127.0.0.1:8787/todos
```

标记完成或重新打开：

```bash
curl -X PATCH http://127.0.0.1:8787/todos/todo_xxx \
  -H 'Content-Type: application/json' \
  -d '{"state":"completed"}'
```

把 `completed` 改为 `active` 即可重新纳入后续总待办。

## 测试

```bash
cd /Users/new/Documents/ChatGPT/workless/action-inbox-service
.venv/bin/pytest -q
```
