import subprocess
from datetime import date
from types import SimpleNamespace

import pytest

from app import digest
from app import main
from app.digest import BaseRecord, DigestConfigError, DigestReport, MockDocumentDraft, TodoItem


def test_read_all_base_records_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        {
            "ok": True,
            "data": {
                "fields": ["待办", "来源"],
                "data": [["确认货盘", "TRAE"]],
                "record_id_list": ["rec1"],
                "has_more": True,
            },
        },
        {
            "ok": True,
            "data": {
                "fields": ["待办", "来源"],
                "data": [["回复客户", "飞书"]],
                "record_id_list": ["rec2"],
                "has_more": False,
            },
        },
    ]
    commands: list[list[str]] = []

    def fake_run(command: list[str], timeout: int = 60, **kwargs: object) -> dict:
        commands.append(command)
        return pages.pop(0)

    monkeypatch.setattr(digest, "_run_lark", fake_run)
    records = digest.read_all_base_records(
        base_token="app_xxx",
        table_id="tblxxx",
        identity="bot",
        field_names=["待办", "来源"],
    )

    assert [record.record_id for record in records] == ["rec1", "rec2"]
    assert records[1].fields == {"待办": "回复客户", "来源": "飞书"}
    assert commands[0][commands[0].index("--offset") + 1] == "0"
    assert commands[1][commands[1].index("--offset") + 1] == "1"


def test_render_digest_groups_priority() -> None:
    report = DigestReport(
        summary="先处理一项已逾期的外部承诺。",
        items=[
            TodoItem(
                title="确认巴黎贝甜七夕货盘",
                intent="task",
                priority="P0",
                due_date="2026-08-15",
                category="工作",
                reason="截止日期已过",
                next_action="确认货盘是否完成并同步结果",
                evidence=["8/15 前给我"],
                source_record_ids=["rec1"],
            )
        ],
        risks=[],
        ignored_count=0,
    )

    message = digest.render_digest(report, generated_on=date(2026, 8, 16))

    assert "总待办 · 2026-08-16" in message
    assert "P0 1" in message
    assert "确认巴黎贝甜七夕货盘" in message
    assert "截止 2026-08-15" in message
    assert "【你需要做的】：确认货盘是否完成并同步结果" in message


def test_format_task_steps_returns_only_the_minimum_action() -> None:
    item = TodoItem(
        title="一证多址认领",
        intent="task",
        priority="P2",
        due_date=None,
        category="申请",
        reason="test",
        next_action="确认承诺函模板",
        task_steps=["确认承诺函模板", "准备门店照片", "提交人工审核"],
        evidence=[],
        source_record_ids=["rec1"],
    )

    assert digest.format_task_steps(item) == "确认承诺函模板"


def test_build_todo_card_contains_task_answer_and_links() -> None:
    item = TodoItem(
        title="一证多址认领",
        intent="task",
        priority="P2",
        due_date=None,
        category="申请",
        reason="test",
        next_action="确认承诺函模板",
        task_steps=["确认承诺函模板", "准备门店照片"],
        evidence=[],
        source_record_ids=["rec1"],
        reference_answer="已知信息：存在一证多址。\n建议方案：准备材料。\n待确认：材料格式。",
    )

    card = digest.build_todo_card(
        item,
        digest_table_url="https://my.feishu.cn/base/example",
        mock_document_url="http://127.0.0.1:8787/documents/todo_test",
    )

    assert card["header"]["title"]["content"] == "一证多址认领"
    assert "**你需要做的**\n确认承诺函模板" in card["elements"][0]["text"]["content"]
    assert "**参考回答**" in card["elements"][0]["text"]["content"]
    assert card["elements"][1]["actions"][0]["url"].endswith("/documents/todo_test")
    assert card["elements"][1]["actions"][1]["url"].startswith(
        "https://my.feishu.cn/base/"
    )


def test_build_digest_card_shows_only_top_three_items_and_document_links() -> None:
    items = [
        TodoItem(
            title=f"事项{i}",
            intent="task",
            priority="P0" if i == 1 else "P2",
            due_date=None,
            category="测试",
            reason="test",
            next_action=f"执行事项{i}",
            evidence=[],
            source_record_ids=[f"rec{i}"],
            todo_id=f"todo_{i}",
            reference_answer=f"参考回答{i}",
        )
        for i in range(1, 5)
    ]
    report = DigestReport(summary="test", items=items, risks=[], ignored_count=0)

    card = digest.build_digest_card(
        report,
        digest_table_url="https://my.feishu.cn/base/example",
        document_urls={item.todo_id: f"http://127.0.0.1/docs/{item.todo_id}" for item in items},
    )

    content = card["elements"][0]["text"]["content"]
    assert card["header"]["title"]["content"] == "今日总待办 · 3 项"
    assert "事项1" in content and "事项3" in content
    assert "事项4" not in content
    assert "还有 1 条待办，请在总表查看。" in content
    assert "待办明细：执行事项1" in content
    assert "参考回复：参考回答1" in content
    assert "[查看资料](http://127.0.0.1/docs/todo_1)" in content
    assert card["elements"][1]["actions"][1]["value"] == {
        "action": "complete",
        "todo_id": "todo_1",
    }


def test_analyze_records_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(DigestConfigError, match="DEEPSEEK_API_KEY"):
        digest.analyze_records(
            [BaseRecord(record_id="rec1", fields={"待办": "回复客户"})],
            model="deepseek-chat",
        )


def test_analyze_records_uses_deepseek_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="""{
                              \"summary\": \"尽快确认货盘。\",
                              \"items\": [{
                                \"title\": \"确认货盘\",
                                \"intent\": \"task\",
                                \"priority\": \"P1\",
                                \"due_date\": \"2026-08-17\",
                                \"category\": \"工作\",
                                \"reason\": \"存在明确截止日期\",
                                \"next_action\": \"确认货盘\",
                                \"evidence\": [\"明天确认\"],
                                \"source_record_ids\": [\"rec1\"]
                              }],
                              \"risks\": [],
                              \"ignored_count\": 0
                            }"""
                        )
                    )
                ]
            )

    class FakeClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(digest, "OpenAI", lambda **kwargs: FakeClient())
    report = digest.analyze_records(
        [BaseRecord(record_id="rec1", fields={"待办": "明天确认货盘"})],
        model="deepseek-chat",
        today=date(2026, 8, 16),
    )

    assert report.items[0].title == "确认货盘"
    assert captured["model"] == "deepseek-chat"
    assert captured["response_format"] == {"type": "json_object"}
    assert "json" in captured["messages"][0]["content"].lower()


def test_local_todo_id_is_stable_for_source_records() -> None:
    report = DigestReport(
        summary="test",
        items=[
            TodoItem(
                title="准备材料并推进申请",
                intent="task",
                priority="P2",
                due_date=None,
                category="申请",
                reason="原始记录包含材料与目标",
                next_action="准备承诺函和照片",
                evidence=["提供承诺函和照片"],
                source_record_ids=["rec2", "rec1"],
            )
        ],
        risks=[],
        ignored_count=0,
    )

    digest.assign_local_todo_ids(report)
    first_id = report.items[0].todo_id
    report.items[0].source_record_ids = ["rec1", "rec2"]
    digest.assign_local_todo_ids(report)

    assert first_id == report.items[0].todo_id


def test_priority_guardrail_downgrades_unsupported_p1() -> None:
    report = DigestReport(
        summary="test",
        items=[
            TodoItem(
                title="准备申请材料",
                intent="task",
                priority="P1",
                due_date=None,
                category="申请",
                reason="当前工作推进事项",
                next_action="准备承诺函",
                evidence=["提供承诺函"],
                source_record_ids=["rec1"],
            )
        ],
        risks=[],
        ignored_count=0,
    )
    records = [BaseRecord(record_id="rec1", fields={"待办": "提供承诺函申请审核"})]

    digest.enforce_priority_guardrails(report, records, date(2026, 8, 16))

    assert report.items[0].priority == "P2"
    assert "已按优先级规则调整为 P2" in report.items[0].reason


def test_materialize_mock_document_keeps_source_and_ai_draft_separate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    report = DigestReport(
        summary="test",
        items=[
            TodoItem(
                title="准备承诺函和照片并推进认领",
                intent="task",
                priority="P2",
                due_date=None,
                category="门店认领",
                reason="原始记录提出材料和人工审核问题",
                next_action="整理承诺函和门店照片后向审核方提交咨询",
                evidence=["提供承诺函，外加照片"],
                source_record_ids=["rec1"],
                reference_answer=(
                    "已知信息：当前遇到一证多址。\n"
                    "建议方案：建议准备材料后询问人工审核入口。\n"
                    "待确认：审核方是否接受该材料组合。"
                ),
                mock_document=MockDocumentDraft(
                    title="工作草稿：一证多址认领材料清单",
                    content_markdown=(
                        "## 已知事实\n- 当前遇到一证多址。\n\n"
                        "## 建议处理路径\n1. 准备材料。\n\n"
                        "## 待确认信息\n- 审核入口。"
                    ),
                ),
            )
        ],
        risks=[],
        ignored_count=0,
    )
    preview = digest.make_preview(
        [BaseRecord(record_id="rec1", fields={"待办": "提供承诺函，外加照片"})], report
    )
    monkeypatch.setattr(main, "MOCK_DOCUMENT_DIR", tmp_path)
    monkeypatch.setattr(main, "MOCK_DOCUMENT_PUBLIC_URL", "http://127.0.0.1:8787")

    documents = main.materialize_mock_documents(
        preview, [BaseRecord(record_id="rec1", fields={"待办": "提供承诺函，外加照片"})]
    )

    document = next(iter(documents.values()))
    content = open(document["path"], encoding="utf-8").read()
    assert document["url"].endswith(preview.report.items[0].todo_id)
    assert content.startswith("# 执行资料：一证多址认领材料清单")
    assert "## 事实依据" in content
    assert "提供承诺函，外加照片" in content
    assert "建议准备材料后询问人工审核入口" in content


def test_materialize_reference_documents_links_user_provided_demo_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    report = DigestReport(
        summary="test",
        items=[
            TodoItem(
                title="巴黎贝甜货盘",
                intent="task",
                priority="P0",
                due_date=None,
                category="测试",
                reason="test",
                next_action="核实货盘",
                evidence=[],
                source_record_ids=["rec1"],
            ),
            TodoItem(
                title="一证多址认领",
                intent="task",
                priority="P2",
                due_date=None,
                category="测试",
                reason="test",
                next_action="准备材料",
                evidence=[],
                source_record_ids=["rec2"],
            ),
            TodoItem(
                title="品牌联名拓展",
                intent="task",
                priority="P2",
                due_date=None,
                category="测试",
                reason="test",
                next_action="联系品牌",
                evidence=[],
                source_record_ids=["rec3"],
            ),
        ],
        risks=[],
        ignored_count=0,
    )
    preview = digest.make_preview([], report)
    monkeypatch.setattr(main, "MOCK_DOCUMENT_DIR", tmp_path)
    monkeypatch.setattr(main, "MOCK_DOCUMENT_PUBLIC_URL", "http://127.0.0.1:8787")

    documents = main.materialize_reference_documents(preview)

    assert len(documents) == 4
    assert "8 个新品均已填完" in preview.report.items[0].reference_answer
    assert "人工审核申请表" in preview.report.items[1].reference_answer
    assert "沪上阿姨 KP：AA" in preview.report.items[2].reference_answer
    first_document = open(documents[0]["path"], encoding="utf-8").read()
    assert "数据状态：本机演示数据" in first_document


def test_send_digest_message_builds_dry_run_command(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def fake_run(command: list[str], timeout: int = 60, **kwargs: object) -> dict:
        captured.extend(command)
        return {"ok": True, "data": {"dry_run": True}}

    monkeypatch.setattr(digest, "_run_lark", fake_run)
    digest.send_digest_message(
        markdown="## 总待办",
        recipient_type="user",
        recipient_id="ou_xxx",
        identity="bot",
        idempotency_key="run-1",
        dry_run=True,
    )

    assert captured[:3] == ["lark-cli", "im", "+messages-send"]
    assert captured[captured.index("--user-id") + 1] == "ou_xxx"
    assert "--dry-run" in captured


def test_run_lark_rejects_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.CompletedProcess(
        args=["lark-cli"],
        returncode=1,
        stdout="",
        stderr='{"ok":false,"error":{"message":"no permission"}}',
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)

    with pytest.raises(digest.DigestError, match="no permission"):
        digest._run_lark(["lark-cli", "base", "+record-list"])


def test_run_lark_accepts_unwrapped_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.CompletedProcess(
        args=["lark-cli"],
        returncode=0,
        stdout='{"api":[{"method":"POST"}]}',
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)

    payload = digest._run_lark(
        ["lark-cli", "im", "+messages-send", "--dry-run"],
        allow_unwrapped_success=True,
    )

    assert payload["api"][0]["method"] == "POST"
