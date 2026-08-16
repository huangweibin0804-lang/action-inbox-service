import json
import os
import subprocess
import uuid
from hashlib import sha256
from re import search
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field, ValidationError, field_validator


Intent = Literal["task", "follow_up", "schedule", "reference", "ignore"]
Priority = Literal["P0", "P1", "P2", "P3"]


class DigestError(RuntimeError):
    pass


class DigestConfigError(DigestError):
    pass


class BaseRecord(BaseModel):
    record_id: str
    fields: dict[str, object]


class MockDocumentDraft(BaseModel):
    """A clearly-labelled AI working draft, never a source document."""

    title: str = Field(description="Mock 内部资料的简短标题")
    content_markdown: str = Field(
        description="基于原始记录生成的内部工作草稿；必须区分事实、建议和待确认项"
    )

    @field_validator("content_markdown", mode="before")
    @classmethod
    def restore_literal_linebreaks(cls, value: object) -> object:
        return value.replace("\\n", "\n") if isinstance(value, str) else value


class TodoItem(BaseModel):
    title: str = Field(description="简短的事项主题，例如一证多址认领、品牌联名拓展")
    intent: Intent
    priority: Priority
    due_date: str | None = Field(
        default=None,
        description="只在原文存在明确日期或可可靠解析的相对日期时填写，格式 YYYY-MM-DD",
    )
    category: str = Field(description="简短中文分类，例如工作、个人、跟进、会议")
    reason: str = Field(description="优先级与意图判断依据，保持简短")
    next_action: str = Field(description="用户现在可以执行的唯一、具体的下一步动作")
    task_steps: list[str] = Field(
        default_factory=list,
        description="完成该事项所需的分点任务；按实际需要列出，不要求固定数量",
    )
    evidence: list[str] = Field(description="来自输入记录的短证据，不补充输入中不存在的事实")
    source_record_ids: list[str]
    todo_id: str | None = Field(
        default=None,
        description="由服务端根据来源记录生成的本地待办 ID，模型不需要填写",
    )
    reference_answer: str = Field(
        default="",
        description="基于输入信息的参考回答或解决方案，需明确事实、建议和待确认项",
    )
    mock_document: MockDocumentDraft | None = Field(
        default=None,
        description="供本地生成的 Mock 内部工作文档；模型不需要提供任何外部链接",
    )

    @field_validator("reference_answer", mode="before")
    @classmethod
    def restore_reference_answer_linebreaks(cls, value: object) -> object:
        return value.replace("\\n", "\n") if isinstance(value, str) else value


class DigestReport(BaseModel):
    summary: str
    items: list[TodoItem]
    risks: list[str]
    ignored_count: int = Field(ge=0)


class DigestPreview(BaseModel):
    run_id: str
    record_count: int
    report: DigestReport
    message_markdown: str


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "todo_digest_system.md"


def _clean_cli_environment() -> dict[str, str]:
    return {
        **os.environ,
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
        "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
    }


def _run_lark(
    command: list[str],
    timeout: int = 60,
    *,
    allow_unwrapped_success: bool = False,
) -> dict:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_clean_cli_environment(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise DigestConfigError("未找到 lark-cli，请先安装并完成飞书应用配置。") from exc
    except subprocess.TimeoutExpired as exc:
        raise DigestError("lark-cli 调用超时。") from exc

    raw = result.stdout.strip() or result.stderr.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        detail = raw[:500] if raw else "无输出"
        raise DigestError(f"lark-cli 返回了无法解析的结果：{detail}") from exc

    envelope_failed = payload.get("ok") is not True and not allow_unwrapped_success
    if result.returncode != 0 or envelope_failed:
        error = payload.get("error", {})
        message = error.get("message") or error.get("hint") or raw[:500]
        raise DigestError(f"飞书调用失败：{message}")
    return payload


def read_all_base_records(
    *,
    base_token: str,
    table_id: str,
    identity: str,
    field_names: list[str],
) -> list[BaseRecord]:
    if not base_token or not table_id:
        raise DigestConfigError("尚未配置 FEISHU_BASE_TOKEN 或 FEISHU_TABLE_ID。")

    records: list[BaseRecord] = []
    offset = 0
    while True:
        command = [
            "lark-cli",
            "base",
            "+record-list",
            "--as",
            identity,
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--limit",
            "200",
            "--offset",
            str(offset),
            "--format",
            "json",
        ]
        for field_name in field_names:
            command.extend(["--field-id", field_name])

        payload = _run_lark(command)
        page = payload.get("data", {})
        field_order = page.get("fields", [])
        rows = page.get("data", [])
        record_ids = page.get("record_id_list", [])
        if not isinstance(field_order, list) or not isinstance(rows, list):
            raise DigestError("多维表返回结构缺少 fields 或 data。")

        for index, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            fields = {
                str(field_order[column]): value
                for column, value in enumerate(row)
                if column < len(field_order)
            }
            record_id = str(record_ids[index]) if index < len(record_ids) else f"offset-{offset + index}"
            records.append(BaseRecord(record_id=record_id, fields=fields))

        if not page.get("has_more"):
            break
        if not rows:
            raise DigestError("多维表分页标记为 has_more，但当前页为空，已停止以避免死循环。")
        offset += len(rows)

    return records


def _analysis_input(records: list[BaseRecord]) -> str:
    compact = [
        {"record_id": record.record_id, **record.fields}
        for record in records
    ]
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def load_system_prompt(*, current_date: date, timezone: str) -> str:
    """Load the editable DeepSeek instruction template for the digest agent."""
    configured_path = os.getenv("DIGEST_PROMPT_FILE", "").strip()
    prompt_path = Path(configured_path).expanduser() if configured_path else DEFAULT_PROMPT_PATH
    try:
        template = prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DigestConfigError(f"未找到待办分析提示词文件：{prompt_path}") from exc

    prompt = (
        template.replace("{{CURRENT_DATE}}", current_date.isoformat())
        .replace("{{TIMEZONE}}", timezone)
        .strip()
    )
    if "json" not in prompt.lower():
        raise DigestConfigError("DeepSeek JSON 模式要求提示词中明确包含 json。")
    return prompt


def analyze_records(
    records: list[BaseRecord],
    *,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    timezone: str = "Asia/Shanghai",
    today: date | None = None,
) -> DigestReport:
    if not records:
        return DigestReport(summary="当前没有待分析记录。", items=[], risks=[], ignored_count=0)
    resolved_api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not resolved_api_key:
        raise DigestConfigError("尚未配置 DEEPSEEK_API_KEY，无法执行意图识别。")

    current_date = today or datetime.now().astimezone().date()
    system_prompt = load_system_prompt(current_date=current_date, timezone=timezone)
    resolved_base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    try:
        max_tokens = int(os.getenv("DEEPSEEK_MAX_TOKENS", "4096"))
    except ValueError as exc:
        raise DigestConfigError("DEEPSEEK_MAX_TOKENS 必须是整数。") from exc

    try:
        client = OpenAI(api_key=resolved_api_key, base_url=resolved_base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _analysis_input(records)},
            ],
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
        )
    except OpenAIError as exc:
        raise DigestError(f"DeepSeek 模型调用失败：{exc}") from exc

    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise DigestError("DeepSeek 没有返回可解析的 JSON 内容。")
    try:
        report = DigestReport.model_validate_json(content)
    except ValidationError as exc:
        raise DigestError(f"DeepSeek 返回的 JSON 不符合待办结构：{exc}") from exc
    allowed_record_ids = {record.record_id for record in records}
    if report.ignored_count > len(records):
        raise DigestError("模型返回的 ignored_count 超过输入记录数。")
    for item in report.items:
        if item.intent not in {"task", "follow_up", "schedule"}:
            raise DigestError(f"模型把 {item.intent} 类型错误地放进了总待办。")
        if not item.source_record_ids:
            raise DigestError("模型返回了没有来源记录的待办。")
        unknown_ids = set(item.source_record_ids) - allowed_record_ids
        if unknown_ids:
            raise DigestError(f"模型返回了不存在的来源记录：{sorted(unknown_ids)}")
        if item.due_date:
            try:
                date.fromisoformat(item.due_date)
            except ValueError as exc:
                raise DigestError(f"模型返回了无效截止日期：{item.due_date}") from exc
    return enforce_priority_guardrails(report, records, current_date)


def enforce_priority_guardrails(
    report: DigestReport, records: list[BaseRecord], current_date: date
) -> DigestReport:
    """Prevent unsupported urgency escalation from reaching the reminder table."""
    source_text_by_id = {
        record.record_id: " ".join(str(value) for value in record.fields.values())
        for record in records
    }
    explicit_blocking_pattern = r"阻塞|卡住|无法推进|无法完成|不处理.*无法|不做.*无法"

    for item in report.items:
        if item.priority != "P1":
            continue
        deadline_is_soon = False
        if item.due_date:
            due_date = date.fromisoformat(item.due_date)
            days_until_due = (due_date - current_date).days
            deadline_is_soon = 0 <= days_until_due <= 3
        source_text = " ".join(
            source_text_by_id.get(record_id, "") for record_id in item.source_record_ids
        )
        has_explicit_blocking = bool(search(explicit_blocking_pattern, source_text))
        if deadline_is_soon or has_explicit_blocking:
            continue
        item.priority = "P2"
        item.reason = (
            f"{item.reason}；原始记录没有近期截止日期或明确阻塞证据，"
            "已按优先级规则调整为 P2。"
        )
    return report


def render_digest(report: DigestReport, *, generated_on: date | None = None) -> str:
    day = generated_on or datetime.now().astimezone().date()
    counts = {priority: 0 for priority in ("P0", "P1", "P2", "P3")}
    for item in report.items:
        counts[item.priority] += 1

    lines = [
        f"## 总待办 · {day.isoformat()}",
        "",
        f"共 {len(report.items)} 项｜P0 {counts['P0']}｜P1 {counts['P1']}｜P2 {counts['P2']}｜P3 {counts['P3']}",
        "",
        report.summary,
    ]

    for priority in ("P0", "P1", "P2", "P3"):
        items = [item for item in report.items if item.priority == priority]
        if not items:
            continue
        lines.extend(["", f"### {priority}"])
        for item in items:
            due = f"｜截止 {item.due_date}" if item.due_date else ""
            lines.append(f"- [ ] {item.title}（{item.category}{due}）")
            lines.append(f"  - 【你需要做的】：{item.next_action}")
            if item.reason:
                lines.append(f"  - 判断：{item.reason}")

    if report.risks:
        lines.extend(["", "### 需确认"])
        lines.extend(f"- {risk}" for risk in report.risks)

    if report.ignored_count:
        lines.extend(["", f"已过滤 {report.ignored_count} 条参考信息或无效记录。"])
    return "\n".join(lines)


def format_task_steps(item: TodoItem) -> str:
    """Return the single most useful next action for concise reminders."""
    return item.next_action


def assign_local_todo_ids(report: DigestReport) -> DigestReport:
    """Use source records as the stable identity for local completion tracking."""
    for item in report.items:
        source_key = "|".join(sorted(set(item.source_record_ids)))
        item.todo_id = f"todo_{sha256(source_key.encode('utf-8')).hexdigest()[:16]}"
    return report


def make_preview(
    records: list[BaseRecord],
    report: DigestReport,
    *,
    completed_source_count: int = 0,
) -> DigestPreview:
    assign_local_todo_ids(report)
    return DigestPreview(
        run_id=str(uuid.uuid4()),
        record_count=len(records),
        report=report,
        message_markdown=render_digest(report),
    )


def _card_text(value: str, *, limit: int) -> str:
    """Keep dynamic model output readable and bounded inside a Feishu card."""
    cleaned = value.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit - 1].rstrip()}…"


def build_todo_card(
    item: TodoItem,
    *,
    digest_table_url: str,
    mock_document_url: str,
) -> dict:
    """Card 1.0 compatibility payload for the configured bot client."""
    palette = {"P0": "red", "P1": "orange", "P2": "blue", "P3": "grey"}[item.priority]
    task_list = _card_text(item.next_action, limit=180)
    reference_answer = _card_text(item.reference_answer or "待生成参考回答。", limit=1600)
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": item.title},
            "template": palette,
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**你需要做的**\n{task_list}\n\n"
                        f"**参考回答**\n{reference_answer}"
                    ),
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看资料"},
                        "type": "primary",
                        "url": mock_document_url,
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开待办总表"},
                        "type": "default",
                        "url": digest_table_url,
                    }
                ],
            },
        ],
    }


def build_digest_card(
    report: DigestReport,
    *,
    digest_table_url: str,
    document_urls: dict[str, str],
    max_items: int = 3,
) -> dict:
    """Build one compact card for the highest-priority actionable todos."""
    shown_items = report.items[:max_items]
    priority = shown_items[0].priority if shown_items else "P3"
    palette = {"P0": "red", "P1": "orange", "P2": "blue", "P3": "grey"}[priority]
    blocks: list[str] = []
    for item in shown_items:
        document_url = document_urls.get(item.todo_id or "")
        link = f"\n[查看资料]({document_url})" if document_url else ""
        blocks.append(
            "\n".join(
                [
                    f"**{item.priority} · {_card_text(item.title, limit=30)}**",
                    f"待办明细：{_card_text(item.next_action, limit=80)}",
                    f"参考回复：{_card_text(item.reference_answer or '待生成参考回答。', limit=800)}{link}",
                ]
            )
        )
    if not blocks:
        blocks.append("当前没有待办。")
    remaining = len(report.items) - len(shown_items)
    if remaining > 0:
        blocks.append(f"还有 {remaining} 条待办，请在总表查看。")
    actions = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开待办总表"},
            "type": "primary",
            "url": digest_table_url,
        }
    ]
    actions.extend(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"完成：{_card_text(item.title, limit=18)}"},
            "type": "default",
            "name": "complete_todo",
            "value": {"action": "complete", "todo_id": item.todo_id},
        }
        for item in shown_items
        if item.todo_id
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"今日总待办 · {len(shown_items)} 项"},
            "template": palette,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(blocks)}},
            {
                "tag": "action",
                "actions": actions,
            },
        ],
    }


def send_interactive_card(
    *,
    card: dict,
    recipient_type: Literal["user", "chat"],
    recipient_id: str,
    identity: Literal["user", "bot"],
    idempotency_key: str,
    dry_run: bool = False,
) -> dict:
    if not recipient_id:
        raise DigestConfigError("尚未配置 FEISHU_RECIPIENT_ID。")
    receive_id_type = "open_id" if recipient_type == "user" else "chat_id"
    command = [
        "lark-cli",
        "api",
        "POST",
        "/open-apis/im/v1/messages",
        "--as",
        identity,
        "--params",
        json.dumps({"receive_id_type": receive_id_type}, ensure_ascii=False),
        "--data",
        json.dumps(
            {
                "receive_id": recipient_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
                "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key)),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "--format",
        "json",
    ]
    if dry_run:
        command.append("--dry-run")
    return _run_lark(command, allow_unwrapped_success=dry_run)


def send_digest_message(
    *,
    markdown: str,
    recipient_type: Literal["user", "chat"],
    recipient_id: str,
    identity: Literal["user", "bot"],
    idempotency_key: str,
    dry_run: bool = False,
) -> dict:
    if not recipient_id:
        raise DigestConfigError("尚未配置 FEISHU_RECIPIENT_ID。")
    recipient_flag = "--user-id" if recipient_type == "user" else "--chat-id"
    command = [
        "lark-cli",
        "im",
        "+messages-send",
        "--as",
        identity,
        recipient_flag,
        recipient_id,
        "--markdown",
        markdown,
        "--idempotency-key",
        idempotency_key,
        "--format",
        "json",
    ]
    if dry_run:
        command.append("--dry-run")
    return _run_lark(command, allow_unwrapped_success=dry_run)
