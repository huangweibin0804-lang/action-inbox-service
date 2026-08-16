import asyncio
import json
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from app.digest import (
    DigestConfigError,
    DigestError,
    DigestPreview,
    DigestReport,
    analyze_records,
    build_digest_card,
    build_todo_card,
    format_task_steps,
    make_preview,
    read_all_base_records,
    send_interactive_card,
    send_digest_message,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_DB_PATH = Path.home() / "Documents" / "ActionInbox" / "action_inbox.db"
DB_PATH = Path(os.getenv("ACTION_INBOX_DB_PATH", DEFAULT_DB_PATH)).expanduser()
FEISHU_BASE_TOKEN = os.getenv("FEISHU_BASE_TOKEN", "").strip()
FEISHU_TABLE_ID = os.getenv("FEISHU_TABLE_ID", "").strip()
FEISHU_DIGEST_TABLE_ID = os.getenv("FEISHU_DIGEST_TABLE_ID", "").strip()
FEISHU_DIGEST_VIEW_ID = os.getenv("FEISHU_DIGEST_VIEW_ID", "").strip()
FEISHU_DIGEST_TABLE_URL = os.getenv("FEISHU_DIGEST_TABLE_URL", "").strip()
FEISHU_IDENTITY = os.getenv("FEISHU_IDENTITY", "bot").strip()
FEISHU_CONTENT_FIELD = os.getenv("FEISHU_CONTENT_FIELD", "待办").strip()
FEISHU_DATE_FIELD = os.getenv("FEISHU_DATE_FIELD", "日期").strip()
FEISHU_TIME_FIELD = os.getenv("FEISHU_TIME_FIELD", "时间").strip()
FEISHU_SOURCE_FIELD = os.getenv("FEISHU_SOURCE_FIELD", "来源").strip()
FEISHU_RECIPIENT_TYPE = os.getenv("FEISHU_RECIPIENT_TYPE", "user").strip()
FEISHU_RECIPIENT_ID = os.getenv("FEISHU_RECIPIENT_ID", "").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
MOCK_DOCUMENT_DIR = Path(
    os.getenv("MOCK_DOCUMENT_DIR", PROJECT_ROOT / "data" / "mock_documents")
).expanduser()
MOCK_DOCUMENT_PUBLIC_URL = os.getenv(
    "MOCK_DOCUMENT_PUBLIC_URL", "http://127.0.0.1:8787"
).strip().rstrip("/")
AUTO_DIGEST_ENABLED = os.getenv("AUTO_DIGEST_ENABLED", "true").strip().lower() == "true"
AUTO_DIGEST_POLL_SECONDS = int(os.getenv("AUTO_DIGEST_POLL_SECONDS", "5"))
AUTO_DIGEST_DEBOUNCE_SECONDS = int(os.getenv("AUTO_DIGEST_DEBOUNCE_SECONDS", "30"))
EVENT_LISTENER_ENABLED = os.getenv("EVENT_LISTENER_ENABLED", "true").strip().lower() == "true"
_background_tasks: list[asyncio.Task] = []

app = FastAPI(title="Action Inbox Sync Service", version="0.2.0")


class CaptureIn(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1)
    captured_at: str
    source: str = Field(default="未知", max_length=100)
    status: str = Field(default="inbox", max_length=50)


class SyncResult(BaseModel):
    status: Literal["synced", "pending", "failed"]
    message: str
    record_id: str | None = None


class DigestSendIn(BaseModel):
    confirm_send: bool = False
    confirm_recipient_id: str = Field(min_length=1)
    confirm_identity: Literal["user", "bot"]
    dry_run: bool = False


class TodoStateUpdateIn(BaseModel):
    state: Literal["active", "completed"]


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS captures (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                source TEXT NOT NULL,
                local_status TEXT NOT NULL,
                sync_status TEXT NOT NULL DEFAULT 'pending',
                feishu_record_id TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todo_exports (
                todo_id TEXT PRIMARY KEY,
                feishu_record_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_todo_states (
                todo_id TEXT PRIMARY KEY,
                source_record_ids TEXT NOT NULL,
                title TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS digest_runs (
                run_id TEXT PRIMARY KEY,
                record_count INTEGER NOT NULL,
                report_json TEXT NOT NULL,
                message_markdown TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'preview',
                delivery_result_json TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_state (
                state_key TEXT PRIMARY KEY,
                source_fingerprint TEXT,
                pending_at TEXT,
                last_sent_fingerprint TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )


@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def source_records_fingerprint(records: list) -> str:
    payload = [
        {"record_id": record.record_id, "fields": record.fields}
        for record in sorted(records, key=lambda record: record.record_id)
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def get_automation_state(state_key: str = "digest") -> dict | None:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM automation_state WHERE state_key = ?", (state_key,)
        ).fetchone()
    return dict(row) if row else None


def save_automation_state(
    *,
    source_fingerprint: str | None = None,
    pending_at: str | None = None,
    last_sent_fingerprint: str | None = None,
    state_key: str = "digest",
) -> None:
    current = get_automation_state(state_key) or {}
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO automation_state (
                state_key, source_fingerprint, pending_at, last_sent_fingerprint, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                source_fingerprint = excluded.source_fingerprint,
                pending_at = excluded.pending_at,
                last_sent_fingerprint = excluded.last_sent_fingerprint,
                updated_at = excluded.updated_at
            """,
            (
                state_key,
                source_fingerprint if source_fingerprint is not None else current.get("source_fingerprint"),
                pending_at,
                last_sent_fingerprint
                if last_sent_fingerprint is not None
                else current.get("last_sent_fingerprint"),
                utc_now(),
            ),
        )


def schedule_auto_digest_refresh() -> None:
    if not AUTO_DIGEST_ENABLED:
        return
    due = datetime.now().astimezone() + timedelta(seconds=AUTO_DIGEST_DEBOUNCE_SECONDS)
    state = get_automation_state() or {}
    save_automation_state(
        source_fingerprint=state.get("source_fingerprint"),
        pending_at=due.isoformat(timespec="seconds"),
        last_sent_fingerprint=state.get("last_sent_fingerprint"),
    )


def base_date(value: str) -> str:
    """Return the local calendar date for the 飞书 '日期' field."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d")
    except ValueError:
        return value


def base_time(value: str) -> str:
    """Return a minute-precise local time for the 飞书 '时间' field."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%H:%M")
    except ValueError:
        return ""


def feishu_is_configured() -> bool:
    return bool(FEISHU_BASE_TOKEN and FEISHU_TABLE_ID)


def run_feishu_sync(capture: CaptureIn) -> SyncResult:
    if not feishu_is_configured():
        return SyncResult(
            status="pending",
            message="尚未配置 FEISHU_BASE_TOKEN 和 FEISHU_TABLE_ID，记录已进入本地待同步队列。",
        )

    fields = {
        "待办": capture.content,
        "日期": base_date(capture.captured_at),
        "时间": base_time(capture.captured_at),
        "来源": capture.source,
    }
    command = [
        "lark-cli",
        "base",
        "+record-upsert",
        "--as",
        FEISHU_IDENTITY,
        "--base-token",
        FEISHU_BASE_TOKEN,
        "--table-id",
        FEISHU_TABLE_ID,
        "--json",
        json.dumps(fields, ensure_ascii=False),
    ]
    environment = {
        **os.environ,
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
        "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
    }
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "飞书写入失败"
        return SyncResult(status="failed", message=detail[:500])

    record_id = None
    try:
        response = json.loads(result.stdout)
        data = response.get("data", {})
        record = data.get("record", {})
        record_id = (
            data.get("record_id")
            or data.get("_record_id")
            or record.get("record_id")
            or record.get("_record_id")
            or record.get("id")
        )
    except (json.JSONDecodeError, AttributeError):
        pass

    return SyncResult(status="synced", message="已同步至飞书多维表。", record_id=record_id)


def persist_result(capture: CaptureIn, sync: SyncResult) -> None:
    now = utc_now()
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE captures
            SET sync_status = ?, feishu_record_id = COALESCE(?, feishu_record_id),
                last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                sync.status,
                sync.record_id,
                None if sync.status == "synced" else sync.message,
                now,
                capture.id,
            ),
        )


def row_to_capture(row: sqlite3.Row) -> dict:
    return dict(row)


def digest_field_names() -> list[str]:
    return list(
        dict.fromkeys(
            [
                FEISHU_CONTENT_FIELD,
                FEISHU_DATE_FIELD,
                FEISHU_TIME_FIELD,
                FEISHU_SOURCE_FIELD,
            ]
        )
    )


def save_digest_preview(preview: DigestPreview) -> None:
    sync_local_todo_states(preview)
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO digest_runs (
                run_id, record_count, report_json, message_markdown,
                delivery_status, created_at
            ) VALUES (?, ?, ?, ?, 'preview', ?)
            """,
            (
                preview.run_id,
                preview.record_count,
                preview.report.model_dump_json(),
                preview.message_markdown,
                utc_now(),
            ),
        )


def get_digest_run(run_id: str) -> sqlite3.Row:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM digest_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="未找到该待办预览。")
    return row


def update_digest_delivery(run_id: str, status: str, result: dict) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE digest_runs
            SET delivery_status = ?, delivery_result_json = ?, sent_at = ?
            WHERE run_id = ?
            """,
            (status, json.dumps(result, ensure_ascii=False), utc_now(), run_id),
        )


def completed_source_record_ids() -> set[str]:
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT source_record_ids FROM local_todo_states WHERE state = 'completed'"
        ).fetchall()
    completed_ids: set[str] = set()
    for row in rows:
        try:
            completed_ids.update(json.loads(row["source_record_ids"]))
        except (TypeError, json.JSONDecodeError):
            continue
    return completed_ids


def sync_local_todo_states(preview: DigestPreview) -> None:
    now = utc_now()
    with db_connection() as conn:
        for item in preview.report.items:
            if not item.todo_id:
                continue
            conn.execute(
                """
                INSERT INTO local_todo_states (
                    todo_id, source_record_ids, title, state, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                ON CONFLICT(todo_id) DO UPDATE SET
                    source_record_ids = excluded.source_record_ids,
                    title = excluded.title,
                    updated_at = excluded.updated_at
                """,
                (
                    item.todo_id,
                    json.dumps(item.source_record_ids, ensure_ascii=False),
                    item.title,
                    now,
                    now,
                ),
            )


def list_local_todos(state: str | None = None) -> list[dict]:
    query = "SELECT * FROM local_todo_states"
    parameters: tuple[str, ...] = ()
    if state:
        query += " WHERE state = ?"
        parameters = (state,)
    query += " ORDER BY CASE state WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC"
    with db_connection() as conn:
        rows = conn.execute(query, parameters).fetchall()
    return [dict(row) for row in rows]


def update_local_todo_state(todo_id: str, state: Literal["active", "completed"]) -> dict:
    now = utc_now()
    completed_at = now if state == "completed" else None
    with db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE local_todo_states
            SET state = ?, completed_at = ?, updated_at = ?
            WHERE todo_id = ?
            """,
            (state, completed_at, now, todo_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="未找到该本地待办。")
        row = conn.execute(
            "SELECT * FROM local_todo_states WHERE todo_id = ?", (todo_id,)
        ).fetchone()
    result = dict(row)
    schedule_auto_digest_refresh()
    return result


def run_lark_json(command: list[str], timeout: int = 20) -> dict:
    environment = {
        **os.environ,
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
        "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
    }
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DigestError(f"飞书待办表同步失败：{error}") from error
    raw = result.stdout.strip() or result.stderr.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise DigestError(f"飞书待办表返回无法解析：{raw[:300]}") from error
    if result.returncode != 0 or payload.get("ok") is not True:
        detail = payload.get("error", {}).get("message") or raw[:300]
        raise DigestError(f"飞书待办表同步失败：{detail}")
    return payload


def todo_export_record_id(todo_id: str) -> str | None:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT feishu_record_id FROM todo_exports WHERE todo_id = ?", (todo_id,)
        ).fetchone()
    return row["feishu_record_id"] if row else None


def find_feishu_todo_record_id(todo_id: str) -> str | None:
    """Recover the Base record ID when the write response omits it."""
    command = [
        "lark-cli",
        "base",
        "+record-list",
        "--as",
        FEISHU_IDENTITY,
        "--base-token",
        FEISHU_BASE_TOKEN,
        "--table-id",
        FEISHU_DIGEST_TABLE_ID,
        "--filter-json",
        json.dumps(
            {"logic": "and", "conditions": [["本地待办ID", "==", todo_id]]},
            ensure_ascii=False,
        ),
        "--limit",
        "2",
        "--format",
        "json",
    ]
    payload = run_lark_json(command)
    record_ids = payload.get("data", {}).get("record_id_list", [])
    if len(record_ids) > 1:
        raise DigestError(f"本地待办 {todo_id} 在飞书输出表中存在重复记录。")
    return str(record_ids[0]) if record_ids else None


def save_todo_export(todo_id: str, feishu_record_id: str) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO todo_exports (todo_id, feishu_record_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(todo_id) DO UPDATE SET
                feishu_record_id = excluded.feishu_record_id,
                updated_at = excluded.updated_at
            """,
            (todo_id, feishu_record_id, utc_now()),
        )


def current_todo_states() -> dict[str, str]:
    with db_connection() as conn:
        rows = conn.execute("SELECT todo_id, state FROM local_todo_states").fetchall()
    return {row["todo_id"]: row["state"] for row in rows}


def digest_table_url() -> str:
    if FEISHU_DIGEST_TABLE_URL:
        return FEISHU_DIGEST_TABLE_URL
    if not FEISHU_BASE_TOKEN or not FEISHU_DIGEST_TABLE_ID:
        return ""
    url = f"https://my.feishu.cn/base/{FEISHU_BASE_TOKEN}?table={FEISHU_DIGEST_TABLE_ID}"
    return f"{url}&view={FEISHU_DIGEST_VIEW_ID}" if FEISHU_DIGEST_VIEW_ID else url


def mock_document_url(todo_id: str) -> str:
    return f"{MOCK_DOCUMENT_PUBLIC_URL}/documents/{todo_id}"


def reference_document_url(todo_id: str, document_key: str) -> str:
    return f"{MOCK_DOCUMENT_PUBLIC_URL}/reference-documents/{todo_id}/{document_key}"


def write_reference_document(
    *, todo_id: str, document_key: str, title: str, content: str
) -> dict[str, str]:
    document_path = MOCK_DOCUMENT_DIR / "reference_documents" / todo_id / f"{document_key}.md"
    try:
        document_path.parent.mkdir(parents=True, exist_ok=True)
        document_path.write_text(
            "\n".join([f"# {title}", "", "数据状态：本机演示数据，需以权限内真实资料复核。", "", content.strip(), ""]),
            encoding="utf-8",
        )
    except OSError as error:
        raise DigestError(f"无法写入参考资料 {title}：{error}") from error
    return {
        "title": title,
        "path": str(document_path),
        "url": reference_document_url(todo_id, document_key),
    }


def materialize_reference_documents(preview: DigestPreview) -> list[dict[str, str]]:
    """Create explicit local fixtures only for the user-provided demo scenarios."""
    documents: list[dict[str, str]] = []
    for item in preview.report.items:
        if not item.todo_id:
            continue
        if "巴黎贝甜" in item.title:
            checklist = write_reference_document(
                todo_id=item.todo_id,
                document_key="product-checklist",
                title="巴黎贝甜七夕新品货盘核对表",
                content=(
                    "## 核对结果\n"
                    "- 新品数量：8\n"
                    "- 填写状态：8 个新品均已填完\n\n"
                    "## 明细\n"
                    "| 序号 | 新品 | 填写状态 |\n| --- | --- | --- |\n"
                    + "\n".join(f"| {index} | 新品{index:02d} | 已填完 |" for index in range(1, 9))
                ),
            )
            documents.append(checklist)
            item.reference_answer = (
                f"已扫描 [{checklist['title']}]({checklist['url']})："
                "当前 8 个新品均已填完；请以真实表格复核后确认货盘状态。"
            )
        elif "一证多址" in item.title:
            guide = write_reference_document(
                todo_id=item.todo_id,
                document_key="identity-verification-guide",
                title="门店认领材料指引",
                content=(
                    "## 申请材料\n"
                    "- 门头拍摄的手举身份证照片\n"
                    "- 门店承诺函\n\n"
                    "## 提交要求\n"
                    "- 照片需清晰展示门头、本人和身份证信息。\n"
                    "- 承诺函需按审核要求补充签章或盖章信息。"
                ),
            )
            form = write_reference_document(
                todo_id=item.todo_id,
                document_key="manual-review-form",
                title="人工审核申请表",
                content=(
                    "## 表单字段\n"
                    "- 门店名称\n- 门店地址\n- 一证多址说明\n- 承诺函附件\n- 门头手举身份证照片附件\n\n"
                    "## 当前状态\n- 演示待提交"
                ),
            )
            documents.extend([guide, form])
            item.reference_answer = (
                f"依据 [{guide['title']}]({guide['url']})，商家需提供门头拍摄的手举身份证照片和承诺函；"
                f"通过 [{form['title']}]({form['url']}) 提交申请。"
            )
        elif "品牌联名" in item.title:
            contacts = write_reference_document(
                todo_id=item.todo_id,
                document_key="brand-contact-directory",
                title="品牌合作联系人表",
                content=(
                    "## 联系人\n"
                    "| 品牌 | KP | 电话 |\n| --- | --- | --- |\n"
                    "| 沪上阿姨 | AA | XXXXX |\n"
                    "| 古茗 | BB | XXXXX |\n"
                    "| 瑞幸 | CC | XXXXX |\n"
                    "| 霸王茶姬 | DD | XXXXX |"
                ),
            )
            documents.append(contacts)
            item.reference_answer = (
                f"依据 [{contacts['title']}]({contacts['url']})：\n"
                "沪上阿姨 KP：AA，电话：XXXXX\n"
                "古茗 KP：BB，电话：XXXXX\n"
                "瑞幸 KP：CC，电话：XXXXX\n"
                "霸王茶姬 KP：DD，电话：XXXXX\n"
                "推进合作，同时关注树夏、奈雪、林里等现有情况。"
            )
    return documents


def materialize_mock_documents(
    preview: DigestPreview, records: list
) -> dict[str, dict[str, str]]:
    """Write formal local working documents and return their direct URLs."""
    try:
        MOCK_DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DigestError(f"无法创建 Mock 文档目录：{error}") from error

    records_by_id = {record.record_id: record for record in records}
    documents: dict[str, dict[str, str]] = {}
    for item in preview.report.items:
        if not item.todo_id:
            continue
        source_lines: list[str] = []
        for record_id in item.source_record_ids:
            record = records_by_id.get(record_id)
            if not record:
                continue
            values = "；".join(
                f"{name}：{value}" for name, value in record.fields.items() if value not in (None, "")
            )
            source_lines.append(f"- `{record_id}`：{values or '原始记录为空'}")

        draft = item.mock_document
        draft_title = draft.title.replace("工作草稿：", "执行资料：") if draft else f"执行资料：{item.title}"
        draft_body = draft.content_markdown if draft else (
            "## 已知事实\n"
            + ("\n".join(source_lines) or "- 暂无可提取的原始事实")
            + "\n\n## 建议处理路径\n"
            + f"1. {item.next_action}\n"
            + "\n## 待确认信息\n- 需要结合实际沟通结果补充。"
        )
        document_text = "\n".join(
            [
                f"# {draft_title}",
                "",
                "## 当前事项",
                item.title,
                "",
                "## 你需要做的",
                item.next_action,
                "",
                "## 事实依据",
                *(source_lines or ["- 未找到关联原始记录。"]),
                "",
                "## 参考回答",
                item.reference_answer or "待生成参考回答。",
                "",
                draft_body.strip(),
                "",
            ]
        )
        document_path = MOCK_DOCUMENT_DIR / f"{item.todo_id}.md"
        try:
            document_path.write_text(document_text, encoding="utf-8")
        except OSError as error:
            raise DigestError(f"无法写入 Mock 文档 {item.todo_id}：{error}") from error
        documents[item.todo_id] = {
            "title": draft_title,
            "path": str(document_path),
            "url": mock_document_url(item.todo_id),
        }
    return documents


def digest_output_field_names() -> dict[str, str]:
    """Use the concise columns when available, with a safe legacy fallback."""
    command = [
        "lark-cli",
        "base",
        "+field-list",
        "--as",
        FEISHU_IDENTITY,
        "--base-token",
        FEISHU_BASE_TOKEN,
        "--table-id",
        FEISHU_DIGEST_TABLE_ID,
        "--format",
        "json",
    ]
    payload = run_lark_json(command)
    names = {
        str(field.get("name"))
        for field in payload.get("data", {}).get("fields", [])
        if field.get("name")
    }
    return {
        "action": "你需要做的" if "你需要做的" in names else "任务清单",
        "document": "资料链接" if "资料链接" in names else "Mock资料链接",
    }


def sync_digest_to_feishu_table(
    preview: DigestPreview, mock_documents: dict[str, dict[str, str]] | None = None
) -> dict:
    if not FEISHU_DIGEST_TABLE_ID:
        return {"status": "skipped", "message": "未配置 FEISHU_DIGEST_TABLE_ID。"}
    if not FEISHU_BASE_TOKEN:
        raise DigestConfigError("未配置 FEISHU_BASE_TOKEN，无法同步今日总待办表。")

    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    mock_documents = mock_documents or {}
    output_fields = digest_output_field_names()
    exports: list[dict] = []
    for item in preview.report.items:
        if not item.todo_id:
            continue
        fields = {
            "待办简称": item.title,
            "优先级": item.priority,
            output_fields["action"]: format_task_steps(item),
            "截止日期": item.due_date,
            "分类": item.category,
            "风险或备注": item.reason,
            "AI参考回答": item.reference_answer,
            output_fields["document"]: mock_documents.get(item.todo_id, {}).get(
                "url", mock_document_url(item.todo_id)
            ),
            "来源记录ID": "、".join(item.source_record_ids),
            "本地待办ID": item.todo_id,
            "生成日期": generated_at,
        }
        command = [
            "lark-cli",
            "base",
            "+record-upsert",
            "--as",
            FEISHU_IDENTITY,
            "--base-token",
            FEISHU_BASE_TOKEN,
            "--table-id",
            FEISHU_DIGEST_TABLE_ID,
            "--json",
            json.dumps(fields, ensure_ascii=False),
            "--format",
            "json",
        ]
        existing_record_id = todo_export_record_id(item.todo_id) or find_feishu_todo_record_id(
            item.todo_id
        )
        if existing_record_id:
            command.extend(["--record-id", existing_record_id])
        payload = run_lark_json(command)
        data = payload.get("data", {})
        record = data.get("record", {})
        record_id = (
            data.get("record_id")
            or record.get("record_id")
            or record.get("id")
            or find_feishu_todo_record_id(item.todo_id)
        )
        if not record_id:
            raise DigestError("飞书待办表同步成功，但未返回 record_id。")
        save_todo_export(item.todo_id, str(record_id))
        exports.append({"todo_id": item.todo_id, "record_id": record_id})
    return {"status": "synced", "count": len(exports), "records": exports}


def deliver_digest_automatically(run_id: str) -> dict:
    """Send one summary card after an explicitly enabled local debounce."""
    row = get_digest_run(run_id)
    report = DigestReport.model_validate_json(row["report_json"])
    result = send_interactive_card(
        card=build_digest_card(
            report,
            digest_table_url=digest_table_url(),
            document_urls={
                item.todo_id or "": mock_document_url(item.todo_id or "")
                for item in report.items
            },
        ),
        recipient_type=FEISHU_RECIPIENT_TYPE,
        recipient_id=FEISHU_RECIPIENT_ID,
        identity=FEISHU_IDENTITY,
        idempotency_key=f"action-inbox-{run_id}-auto-summary-card",
    )
    update_digest_delivery(run_id, "sent", result)
    return result


def complete_todo_from_text(text: str) -> dict | None:
    normalized = text.replace("：", ":").strip()
    if "完成" not in normalized:
        return None
    candidates = [todo for todo in list_local_todos("active") if todo["title"] in normalized]
    if len(candidates) != 1:
        return None
    return update_local_todo_state(candidates[0]["todo_id"], "completed")


def handle_incoming_message(event: dict) -> None:
    if event.get("sender_id") != FEISHU_RECIPIENT_ID or event.get("message_type") != "text":
        return
    todo = complete_todo_from_text(str(event.get("content", "")))
    if not todo:
        return
    send_digest_message(
        markdown=f"已将「{todo['title']}」标记为已完成，30 秒后将发送最新总待办。",
        recipient_type=FEISHU_RECIPIENT_TYPE,
        recipient_id=FEISHU_RECIPIENT_ID,
        identity=FEISHU_IDENTITY,
        idempotency_key=f"action-inbox-message-complete-{event.get('event_id', todo['todo_id'])}",
    )


def handle_card_action(event: dict) -> None:
    if event.get("operator_id") != FEISHU_RECIPIENT_ID:
        return
    try:
        action_value = json.loads(event.get("action_value") or "{}")
    except json.JSONDecodeError:
        return
    if action_value.get("action") != "complete":
        return
    todo_id = action_value.get("todo_id")
    if not isinstance(todo_id, str):
        return
    try:
        todo = update_local_todo_state(todo_id, "completed")
    except HTTPException:
        return
    send_digest_message(
        markdown=f"已将「{todo['title']}」标记为已完成，30 秒后将发送最新总待办。",
        recipient_type=FEISHU_RECIPIENT_TYPE,
        recipient_id=FEISHU_RECIPIENT_ID,
        identity=FEISHU_IDENTITY,
        idempotency_key=f"action-inbox-card-complete-{event.get('event_id', todo_id)}",
    )


async def consume_feishu_event(event_key: str, handler) -> None:
    """Keep one long-connection consumer alive for a configured Feishu event."""
    while True:
        process = await asyncio.create_subprocess_exec(
            "lark-cli",
            "event",
            "consume",
            event_key,
            "--as",
            "bot",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdout is not None
        try:
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await asyncio.to_thread(handler, event)
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
        await asyncio.sleep(5)


async def auto_digest_monitor() -> None:
    """Detect source-table changes, debounce 30 seconds, then send one updated card."""
    while True:
        try:
            records = await asyncio.to_thread(
                read_all_base_records,
                base_token=FEISHU_BASE_TOKEN,
                table_id=FEISHU_TABLE_ID,
                identity=FEISHU_IDENTITY,
                field_names=digest_field_names(),
            )
            fingerprint = source_records_fingerprint(records)
            state = get_automation_state() or {}
            if not state.get("source_fingerprint"):
                save_automation_state(source_fingerprint=fingerprint, pending_at=None)
            elif state.get("source_fingerprint") != fingerprint:
                due = datetime.now().astimezone() + timedelta(seconds=AUTO_DIGEST_DEBOUNCE_SECONDS)
                save_automation_state(source_fingerprint=fingerprint, pending_at=due.isoformat(timespec="seconds"))
            elif state.get("pending_at"):
                due = datetime.fromisoformat(state["pending_at"])
                if datetime.now().astimezone() >= due:
                    preview = await asyncio.to_thread(create_digest_preview)
                    await asyncio.to_thread(deliver_digest_automatically, preview["run_id"])
                    save_automation_state(
                        source_fingerprint=fingerprint,
                        pending_at=None,
                        last_sent_fingerprint=fingerprint,
                    )
        except Exception:
            # The next poll retries; failures remain isolated from the HTTP service.
            pass
        await asyncio.sleep(max(AUTO_DIGEST_POLL_SECONDS, 1))


def raise_digest_http_error(error: DigestError) -> None:
    status_code = 503 if isinstance(error, DigestConfigError) else 502
    raise HTTPException(status_code=status_code, detail=str(error)) from error


@app.on_event("startup")
def startup() -> None:
    init_db()
    if AUTO_DIGEST_ENABLED:
        _background_tasks.append(asyncio.create_task(auto_digest_monitor()))
    if EVENT_LISTENER_ENABLED:
        _background_tasks.extend(
            [
                asyncio.create_task(
                    consume_feishu_event("im.message.receive_v1", handle_incoming_message)
                ),
                asyncio.create_task(
                    consume_feishu_event("card.action.trigger", handle_card_action)
                ),
            ]
        )


@app.on_event("shutdown")
async def shutdown() -> None:
    for task in _background_tasks:
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
    _background_tasks.clear()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "database": str(DB_PATH),
        "feishu_configured": feishu_is_configured(),
        "feishu_identity": FEISHU_IDENTITY,
        "digest": {
            "deepseek_configured": bool(os.getenv("DEEPSEEK_API_KEY", "").strip()),
            "model": DEEPSEEK_MODEL,
            "recipient_configured": bool(FEISHU_RECIPIENT_ID),
            "recipient_type": FEISHU_RECIPIENT_TYPE,
            "output_table_configured": bool(FEISHU_DIGEST_TABLE_ID),
            "mock_document_directory": str(MOCK_DOCUMENT_DIR),
            "mock_document_public_url": MOCK_DOCUMENT_PUBLIC_URL,
            "card_delivery": "interactive_card",
        },
    }


@app.post("/captures")
def create_capture(capture: CaptureIn) -> dict:
    now = utc_now()
    with db_connection() as conn:
        existing = conn.execute("SELECT * FROM captures WHERE id = ?", (capture.id,)).fetchone()
        if existing:
            return {"duplicate": True, "capture": row_to_capture(existing)}

        conn.execute(
            """
            INSERT INTO captures (
                id, content, captured_at, source, local_status, sync_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                capture.id,
                capture.content,
                capture.captured_at,
                capture.source,
                capture.status,
                now,
                now,
            ),
        )

    sync = run_feishu_sync(capture)
    persist_result(capture, sync)
    return {"duplicate": False, "sync": sync.model_dump()}


@app.get("/captures")
def list_captures(limit: int = 50) -> list[dict]:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit 必须在 1 到 200 之间")
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM captures ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [row_to_capture(row) for row in rows]


@app.post("/sync/pending")
def retry_pending_sync() -> dict:
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM captures WHERE sync_status != 'synced' ORDER BY created_at ASC"
        ).fetchall()

    results = []
    for row in rows:
        capture = CaptureIn(
            id=row["id"],
            content=row["content"],
            captured_at=row["captured_at"],
            source=row["source"],
            status=row["local_status"],
        )
        sync = run_feishu_sync(capture)
        persist_result(capture, sync)
        results.append({"id": capture.id, "sync": sync.model_dump()})
    return {"count": len(results), "results": results}


@app.post("/digest/preview")
def create_digest_preview() -> dict:
    try:
        records = read_all_base_records(
            base_token=FEISHU_BASE_TOKEN,
            table_id=FEISHU_TABLE_ID,
            identity=FEISHU_IDENTITY,
            field_names=digest_field_names(),
        )
        completed_ids = completed_source_record_ids()
        active_records = [record for record in records if record.record_id not in completed_ids]
        report = analyze_records(active_records, model=DEEPSEEK_MODEL)
        preview = make_preview(active_records, report)
        reference_documents = materialize_reference_documents(preview)
        save_digest_preview(preview)
        mock_documents = materialize_mock_documents(preview, active_records)
        output_sync = sync_digest_to_feishu_table(preview, mock_documents)
        document_urls = {
            todo_id: document["url"] for todo_id, document in mock_documents.items()
        }
        card_preview = build_digest_card(
            preview.report,
            digest_table_url=digest_table_url(),
            document_urls=document_urls,
        )
        return {
            **preview.model_dump(),
            "completed_source_count": len(completed_ids),
            "mock_documents": list(mock_documents.values()),
            "reference_documents": reference_documents,
            "card_preview": card_preview,
            "output_sync": output_sync,
        }
    except DigestError as error:
        raise_digest_http_error(error)


@app.get("/digest/{run_id}")
def read_digest_preview(run_id: str) -> dict:
    row = get_digest_run(run_id)
    return {
        "run_id": row["run_id"],
        "record_count": row["record_count"],
        "report": json.loads(row["report_json"]),
        "message_markdown": row["message_markdown"],
        "delivery_status": row["delivery_status"],
        "created_at": row["created_at"],
        "sent_at": row["sent_at"],
    }


@app.get("/documents/{todo_id}", response_class=PlainTextResponse)
@app.get("/mock-documents/{todo_id}", response_class=PlainTextResponse, include_in_schema=False)
def read_mock_document(todo_id: str) -> str:
    if not todo_id.startswith("todo_") or "/" in todo_id or "\\" in todo_id:
        raise HTTPException(status_code=400, detail="无效的资料 ID。")
    document_path = MOCK_DOCUMENT_DIR / f"{todo_id}.md"
    try:
        return document_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="尚未生成该执行资料。") from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"无法读取执行资料：{error}") from error


@app.get("/reference-documents/{todo_id}/{document_key}", response_class=PlainTextResponse)
def read_reference_document(todo_id: str, document_key: str) -> str:
    if not todo_id.startswith("todo_") or "/" in todo_id or "\\" in todo_id:
        raise HTTPException(status_code=400, detail="无效的资料 ID。")
    if document_key not in {
        "product-checklist",
        "identity-verification-guide",
        "manual-review-form",
        "brand-contact-directory",
    }:
        raise HTTPException(status_code=404, detail="未找到该参考资料。")
    document_path = MOCK_DOCUMENT_DIR / "reference_documents" / todo_id / f"{document_key}.md"
    try:
        return document_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="尚未生成该参考资料。") from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"无法读取参考资料：{error}") from error


@app.get("/todos")
def get_local_todos(state: Literal["active", "completed"] | None = None) -> list[dict]:
    return list_local_todos(state)


@app.patch("/todos/{todo_id}")
def patch_local_todo(todo_id: str, update: TodoStateUpdateIn) -> dict:
    return update_local_todo_state(todo_id, update.state)


@app.post("/digest/{run_id}/send")
def send_digest(run_id: str, request: DigestSendIn) -> dict:
    row = get_digest_run(run_id)
    if row["delivery_status"] == "sent":
        return {
            "duplicate": True,
            "delivery_status": "sent",
            "result": json.loads(row["delivery_result_json"] or "{}"),
        }
    if FEISHU_RECIPIENT_TYPE not in {"user", "chat"}:
        raise HTTPException(status_code=503, detail="FEISHU_RECIPIENT_TYPE 必须是 user 或 chat。")
    if FEISHU_IDENTITY not in {"user", "bot"}:
        raise HTTPException(status_code=503, detail="FEISHU_IDENTITY 必须是 user 或 bot。")
    if request.confirm_recipient_id != FEISHU_RECIPIENT_ID:
        raise HTTPException(status_code=400, detail="确认的收件人与服务配置不一致。")
    if request.confirm_identity != FEISHU_IDENTITY:
        raise HTTPException(status_code=400, detail="确认的发送身份与服务配置不一致。")
    if not request.dry_run and not request.confirm_send:
        raise HTTPException(status_code=400, detail="实际发送前必须设置 confirm_send=true。")

    try:
        report = DigestReport.model_validate_json(row["report_json"])
        result = send_interactive_card(
            card=build_digest_card(
                report,
                digest_table_url=digest_table_url(),
                document_urls={
                    item.todo_id or "": mock_document_url(item.todo_id or "")
                    for item in report.items
                },
            ),
            recipient_type=FEISHU_RECIPIENT_TYPE,
            recipient_id=FEISHU_RECIPIENT_ID,
            identity=FEISHU_IDENTITY,
            idempotency_key=f"action-inbox-{run_id}-summary-card",
            dry_run=request.dry_run,
        )
    except DigestError as error:
        raise_digest_http_error(error)

    status = "dry_run" if request.dry_run else "sent"
    update_digest_delivery(run_id, status, result)
    return {"duplicate": False, "delivery_status": status, "result": result}
