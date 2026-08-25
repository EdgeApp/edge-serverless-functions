import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FUNCTION = PROJECT_ROOT / "packages/intercom-article-drafts/upload/__main__.py"
PACKAGE_ROOT = PROJECT_ROOT / "packages/intercom-article-drafts"
spec = importlib.util.spec_from_file_location("article_draft_upload", FUNCTION)
upload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upload)
UNSET = object()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("INTERCOM_ACCESS_TOKEN", "shared-intercom-token")
    monkeypatch.setenv("INTERCOM_DRAFT_BRIDGE_SECRET", "bridge-secret-value")
    monkeypatch.setenv("INTERCOM_ARTICLE_AUTHOR_ID", "12345")


def event(payload, token="bridge-secret-value", method="POST"):
    return {
        "http": {
            "method": method,
            "headers": {"Authorization": f"Bearer {token}"},
            "body": json.dumps(payload),
        }
    }


def encoded_event(raw):
    return {
        "http": {
            "method": "POST",
            "headers": {"Authorization": "Bearer bridge-secret-value"},
            "body": raw,
            "isBase64Encoded": True,
        }
    }


def response(data, status=200):
    result = MagicMock()
    result.status_code = status
    result.json.return_value = data
    result.raise_for_status.return_value = None
    return result


def body(result):
    return json.loads(result["body"])


def assert_reconciliation(result, article_id=None):
    assert result["statusCode"] == 502
    payload = body(result)
    assert payload["error"] == "Intercom mutation requires manual reconciliation before retry"
    assert payload["reconciliation_required"] is True
    assert payload["retry_safe"] is False
    assert payload["operation_id"].startswith("kb-test-")
    if article_id is None:
        assert "article_id" not in payload
    else:
        assert payload["article_id"] == article_id


def create_payload(**overrides):
    return {
        "operation": "create",
        "operation_id": "kb-test-create-0001",
        "title": "Reset 2FA",
        "description": "Recovery steps",
        "body_markdown": "# Reset 2FA\n\nDo the thing.",
        **overrides,
    }


def update_payload(**overrides):
    return {
        "operation": "update",
        "operation_id": "kb-test-update-0001",
        "article_id": "9001",
        "title": "Reset 2FA safely",
        "description": "Current recovery steps",
        "body_markdown": "# Reset 2FA safely\n\nDo the safer thing.",
        **overrides,
    }


def article(
    *,
    title="Reset 2FA",
    description="Recovery steps",
    body_markdown="# Reset 2FA\n\nDo the thing.",
    state="draft",
    pending=False,
    author_id=12345,
    updated_at=1700000000,
    draft_updated_at=UNSET,
    scheduled_publish_at=None,
    scheduled_unpublish_at=None,
):
    if draft_updated_at is UNSET:
        draft_updated_at = 1700000100 if pending else None
    return {
        "id": "9001",
        "type": "article",
        "title": title,
        "description": description,
        "body": f"<p>{body_markdown}</p>",
        "body_markdown": body_markdown,
        "state": state,
        "has_unpublished_changes": pending,
        "draft_updated_at": draft_updated_at,
        "updated_at": updated_at,
        "author_id": author_id,
        "scheduled_publish_at": scheduled_publish_at,
        "scheduled_unpublish_at": scheduled_unpublish_at,
    }


def submitted_article(state="draft", pending=False, **overrides):
    return article(
        title="Reset 2FA safely",
        description="Current recovery steps",
        body_markdown="# Reset 2FA safely\n\nDo the safer thing.\n",
        state=state,
        pending=pending,
        **overrides,
    )


def test_manifest_keeps_numeric_author_string_and_allows_update_readbacks():
    manifest = (PROJECT_ROOT / "project.yml").read_text()

    assert re.search(
        r'^\s+INTERCOM_ARTICLE_AUTHOR_ID:\s+"\$\{INTERCOM_ARTICLE_AUTHOR_ID\}"\s*$',
        manifest,
        re.MULTILINE,
    )
    assert re.search(r"^\s+timeout:\s+45000\s*$", manifest, re.MULTILINE)


def test_only_upload_is_a_deployable_article_draft_action():
    immediate_directories = sorted(
        path.name for path in PACKAGE_ROOT.iterdir() if path.is_dir()
    )
    assert immediate_directories == ["upload"]


def test_create_forces_draft_and_returns_identity():
    created = article()
    with patch.object(upload.requests, "request", return_value=response(created)) as request:
        result = upload.main(event(create_payload()), None)

    assert result["statusCode"] == 200
    receipt = body(result)
    assert receipt["ok"] is True
    assert receipt["operation_id"] == "kb-test-create-0001"
    assert receipt["operation"] == "create"
    assert receipt["result"] == "created"
    assert receipt["article_id"] == "9001"
    assert receipt["title"] == "Reset 2FA"
    assert receipt["before_state"] is None
    assert receipt["after_state"] == "draft"
    assert receipt["state"] == "draft"
    assert receipt["draft_mode"] == "new"
    assert re.fullmatch(r"[0-9a-f]{64}", receipt["submitted_content_hash"])
    assert receipt["completed_at"].endswith("Z")
    sent = request.call_args.kwargs["json"]
    headers = request.call_args.kwargs["headers"]
    assert sent["state"] == "draft"
    assert sent["author_id"] == 12345
    assert headers["Authorization"] == "Bearer shared-intercom-token"
    assert request.call_args.args[:2] == (
        "POST",
        "https://api.intercom.io/articles",
    )
    connect_timeout, read_timeout = request.call_args.kwargs["timeout"]
    assert 0 < connect_timeout <= upload.CONNECT_TIMEOUT_SECONDS
    assert 0 < read_timeout <= upload.READ_TIMEOUT_SECONDS


def test_create_accepts_intercom_generated_heading_anchor_in_readback():
    created = article(
        body_markdown="# Reset 2FA {#h_0d8a2f2ea4}\n\nDo the thing.\n"
    )
    with patch.object(upload.requests, "request", return_value=response(created)):
        result = upload.main(event(create_payload()), None)

    assert result["statusCode"] == 200
    assert body(result)["result"] == "created"


@pytest.mark.parametrize(
    "returned_markdown",
    [
        "# Reset 2FA {#custom}\n\nDo the thing.",
        "# Reset 2FA {#h_0d8a2f2ea}\n\nDo the thing.",
        "# Reset 2FA {#h_0d8a2f2ea44}\n\nDo the thing.",
        "# Changed heading {#h_0d8a2f2ea4}\n\nDo the thing.",
        "# Reset 2FA\n\nDo the thing. {#h_0d8a2f2ea4}",
    ],
)
def test_create_rejects_non_intercom_heading_canonicalization(returned_markdown):
    created = article(body_markdown=returned_markdown)
    with patch.object(upload.requests, "request", return_value=response(created)):
        result = upload.main(event(create_payload()), None)

    assert_reconciliation(result, "9001")


def test_create_reconciles_if_intercom_returns_the_wrong_author():
    created = article(author_id=54321)
    with patch.object(upload.requests, "request", return_value=response(created)):
        result = upload.main(event(create_payload()), None)

    assert_reconciliation(result, "9001")


@pytest.mark.parametrize(
    "created",
    [
        article(pending=True),
        article(draft_updated_at=1700000100),
        {key: value for key, value in article().items() if key != "has_unpublished_changes"},
        {key: value for key, value in article().items() if key != "draft_updated_at"},
    ],
)
def test_create_requires_explicit_clean_unpublished_draft_state(created):
    with patch.object(upload.requests, "request", return_value=response(created)):
        result = upload.main(event(create_payload()), None)

    assert_reconciliation(result, "9001")


def test_update_routes_unpublished_article_through_normal_update_and_readback():
    old = article()
    updated = submitted_article()
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(old), response(updated), response(updated)],
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 200
    receipt = body(result)
    assert receipt["operation"] == "update"
    assert receipt["result"] == "updated_draft"
    assert receipt["article_id"] == "9001"
    assert receipt["before_state"] == "draft"
    assert receipt["after_state"] == "draft"
    assert receipt["state"] == "draft"
    assert receipt["draft_mode"] == "unpublished"
    calls = request.call_args_list
    assert [call.args[:2] for call in calls] == [
        ("GET", "https://api.intercom.io/articles/9001"),
        ("PUT", "https://api.intercom.io/articles/9001"),
        ("GET", "https://api.intercom.io/articles/9001"),
    ]
    sent = calls[1].kwargs["json"]
    assert sent["author_id"] == 12345
    assert "state" not in sent


def test_unpublished_update_accepts_intercom_generated_heading_anchor_in_readback():
    old = article()
    updated = article(
        title="Reset 2FA safely",
        description="Current recovery steps",
        body_markdown="# Reset 2FA safely {#h_e8e69bf58e}\n\nDo the safer thing.\n"
    )
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(old), response(updated), response(updated)],
    ):
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 200
    assert body(result)["result"] == "updated_draft"


def test_unpublished_update_reconciles_if_readback_has_the_wrong_author():
    old = article()
    updated = submitted_article(author_id=54321)
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(old), response(updated), response(updated)],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


def test_unpublished_update_rejects_inconsistent_pending_state_before_mutation():
    old = article(pending=True)
    with patch.object(
        upload.requests, "request", return_value=response(old)
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 502
    assert body(result)["error"] == "Intercom reported an inconsistent unpublished-draft state"
    assert request.call_count == 1


def test_unpublished_update_reconciles_if_readback_claims_pending_changes():
    old = article()
    updated = submitted_article(pending=True)
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(old), response(updated), response(updated)],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


def test_update_routes_published_article_through_staged_draft_endpoint():
    live = article(state="published")
    staged = submitted_article(state="published", pending=True)
    live_after = {**live, "has_unpublished_changes": True, "draft_updated_at": 1700000100}
    with patch.object(
        upload.requests,
        "request",
        side_effect=[
            response(live),
            response(live),
            response(staged),
            response(staged),
            response(live_after),
        ],
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 200
    receipt = body(result)
    assert receipt["operation"] == "update"
    assert receipt["result"] == "staged_draft"
    assert receipt["article_id"] == "9001"
    assert receipt["before_state"] == "published"
    assert receipt["after_state"] == "published_with_draft"
    assert receipt["state"] == "published"
    assert receipt["draft_mode"] == "published_revision"
    assert receipt["has_unpublished_changes"] is True
    calls = request.call_args_list
    assert [call.args[:2] for call in calls] == [
        ("GET", "https://api.intercom.io/articles/9001"),
        ("GET", "https://api.intercom.io/articles/9001"),
        ("PUT", "https://api.intercom.io/articles/9001/draft"),
        ("GET", "https://api.intercom.io/articles/9001/draft"),
        ("GET", "https://api.intercom.io/articles/9001"),
    ]
    assert "state" not in calls[2].kwargs["json"]


def test_published_update_accepts_intercom_generated_heading_anchor_in_readback():
    live = article(state="published")
    staged = article(
        title="Reset 2FA safely",
        description="Current recovery steps",
        state="published",
        pending=True,
        body_markdown="# Reset 2FA safely {#h_deadbeef00}\n\nDo the safer thing.\n",
    )
    live_after = {**live, "has_unpublished_changes": True, "draft_updated_at": 1700000100}
    with patch.object(
        upload.requests,
        "request",
        side_effect=[
            response(live),
            response(live),
            response(staged),
            response(staged),
            response(live_after),
        ],
    ):
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 200
    assert body(result)["result"] == "staged_draft"


def test_published_update_reconciles_if_staged_readback_has_the_wrong_author():
    live = article(state="published")
    staged = submitted_article(state="published", pending=True, author_id=54321)
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(live), response(live), response(staged), response(staged)],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


def test_published_update_rejects_existing_staged_revision_before_mutation():
    live = article(state="published", pending=True)
    with patch.object(
        upload.requests, "request", return_value=response(live)
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 409
    assert body(result)["error"] == (
        "Published article already has staged changes; reconcile before update"
    )
    assert request.call_count == 1
    assert request.call_args.args[:2] == (
        "GET",
        "https://api.intercom.io/articles/9001",
    )


def test_published_update_requires_explicit_pending_revision_state():
    live = article(state="published")
    del live["has_unpublished_changes"]
    with patch.object(
        upload.requests, "request", return_value=response(live)
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 502
    assert body(result)["error"] == (
        "Intercom did not report the published article's draft state"
    )
    assert request.call_count == 1


@pytest.mark.parametrize(
    "live",
    [
        article(state="published", draft_updated_at=1700000100),
        article(state="published", pending=True, draft_updated_at=None),
    ],
)
def test_published_update_rejects_inconsistent_pending_revision_state(live):
    with patch.object(
        upload.requests, "request", return_value=response(live)
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 502
    assert body(result)["error"] == "Intercom reported an inconsistent staged-draft state"
    assert request.call_count == 1


@pytest.mark.parametrize(
    "incomplete",
    [
        {"title": None},
        {"body": None, "body_markdown": None},
    ],
)
def test_published_update_rejects_incomplete_live_readback_before_mutation(incomplete):
    live = {**article(state="published"), **incomplete}
    with patch.object(
        upload.requests, "request", return_value=response(live)
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 502
    assert body(result)["error"] == "Intercom live-article readback was incomplete"
    assert request.call_count == 1


@pytest.mark.parametrize("state", ["draft", "published"])
@pytest.mark.parametrize(
    "schedule_field", ["scheduled_publish_at", "scheduled_unpublish_at"]
)
def test_update_rejects_existing_scheduled_target_before_mutation(
    state, schedule_field
):
    existing = article(state=state, **{schedule_field: 1780000000})
    with patch.object(
        upload.requests, "request", return_value=response(existing)
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 409
    assert body(result)["error"] == (
        "Scheduled articles require manual reconciliation before update"
    )
    assert request.call_count == 1


@pytest.mark.parametrize("state", ["draft", "published"])
@pytest.mark.parametrize(
    "schedule_field", ["scheduled_publish_at", "scheduled_unpublish_at"]
)
def test_update_requires_schedule_fields_before_mutation(state, schedule_field):
    existing = article(state=state)
    del existing[schedule_field]
    with patch.object(
        upload.requests, "request", return_value=response(existing)
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 502
    assert body(result)["error"] == "Intercom did not report article schedule state"
    assert request.call_count == 1


def test_create_requires_schedule_fields_in_committed_readback():
    created = article()
    del created["scheduled_unpublish_at"]
    with patch.object(upload.requests, "request", return_value=response(created)):
        result = upload.main(event(create_payload()), None)

    assert_reconciliation(result, "9001")


def test_unpublished_update_requires_schedule_to_remain_clear_on_readback():
    old = article()
    updated = submitted_article(scheduled_publish_at=1780000000)
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(old), response(updated), response(updated)],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


def test_unpublished_update_requires_schedule_fields_on_readback():
    old = article()
    updated = submitted_article()
    del updated["scheduled_publish_at"]
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(old), response(updated), response(updated)],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


def test_staged_update_requires_schedule_to_remain_clear_on_readback():
    live = article(state="published")
    staged = submitted_article(
        state="published", pending=True, scheduled_publish_at=1780000000
    )
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(live), response(live), response(staged), response(staged)],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


def test_staged_update_fails_closed_if_live_article_changes():
    live = article(state="published")
    staged = submitted_article(state="published", pending=True)
    live_after = {**live, "title": "A human changed the live article"}
    live_after.update(has_unpublished_changes=True, draft_updated_at=1700000100)
    with patch.object(
        upload.requests,
        "request",
        side_effect=[
            response(live),
            response(live),
            response(staged),
            response(staged),
            response(live_after),
        ],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


def test_published_update_stops_if_human_stages_after_initial_preflight():
    live = article(state="published")
    gained_draft = article(state="published", pending=True)
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(live), response(gained_draft)],
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 409
    assert body(result)["error"] == (
        "Published article gained staged changes during update preflight"
    )
    assert request.call_count == 2


def test_published_update_stops_if_article_version_changes_during_preflight():
    live = article(state="published")
    changed = article(state="published", updated_at=1700000001)
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(live), response(changed)],
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 409
    assert body(result)["error"] == "Published article changed during update preflight"
    assert request.call_count == 2


def test_published_update_requires_schedule_fields_on_immediate_prewrite_read():
    live = article(state="published")
    incomplete = article(state="published")
    del incomplete["scheduled_unpublish_at"]
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(live), response(incomplete)],
    ) as request:
        result = upload.main(event(update_payload()), None)

    assert result["statusCode"] == 502
    assert body(result)["error"] == "Intercom did not report article schedule state"
    assert request.call_count == 2


def test_published_update_requires_live_readback_to_confirm_staged_revision():
    live = article(state="published")
    staged = submitted_article(state="published", pending=True)
    live_after = article(state="published")
    with patch.object(
        upload.requests,
        "request",
        side_effect=[
            response(live),
            response(live),
            response(staged),
            response(staged),
            response(live_after),
        ],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


def test_published_update_reconciles_if_staged_timestamp_changes_before_final_readback():
    live = article(state="published")
    staged = submitted_article(
        state="published", pending=True, draft_updated_at=1700000100
    )
    live_after = {
        **live,
        "has_unpublished_changes": True,
        "draft_updated_at": 1700000200,
    }
    with patch.object(
        upload.requests,
        "request",
        side_effect=[
            response(live),
            response(live),
            response(staged),
            response(staged),
            response(live_after),
        ],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


@pytest.mark.parametrize(
    "live_change",
    [
        {"state": "draft"},
        {"author_id": 54321},
        {"scheduled_publish_at": 1780000000},
        {"scheduled_unpublish_at": 1780000000},
    ],
)
def test_staged_update_reconciles_live_safety_drift(live_change):
    live = article(state="published")
    staged = submitted_article(state="published", pending=True)
    live_after = {
        **live,
        "has_unpublished_changes": True,
        "draft_updated_at": 1700000100,
        **live_change,
    }
    with patch.object(
        upload.requests,
        "request",
        side_effect=[
            response(live),
            response(live),
            response(staged),
            response(staged),
            response(live_after),
        ],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


def test_unpublished_update_detects_the_accepted_publish_race():
    old = article()
    raced = submitted_article(state="published")
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(old), response(raced), response(raced)],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


@pytest.mark.parametrize("operation", ["publish", "delete", "anything"])
def test_mutating_operations_other_than_create_and_update_are_unreachable(operation):
    with patch.object(upload.requests, "request") as request:
        result = upload.main(event({"operation": operation}), None)
    assert result["statusCode"] == 400
    request.assert_not_called()


@pytest.mark.parametrize(
    "extra",
    [
        {"parent_id": 77, "parent_type": "collection"},
        {"state": "published"},
        {"scheduled_publish_at": "2026-08-25T12:00:00Z"},
        {"scheduled_unpublish_at": "2026-08-25T12:00:00Z"},
        {"ai_chatbot_availability": True},
    ],
)
def test_unsupported_fields_are_rejected_before_intercom(extra):
    with patch.object(upload.requests, "request") as request:
        result = upload.main(event(create_payload(**extra)), None)
    assert result["statusCode"] == 400
    request.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        create_payload(article_id="9001"),
        create_payload(article_id=None),
        update_payload(article_id=None),
        update_payload(article_id="0"),
        update_payload(article_id="0001"),
        update_payload(article_id="١٢٣"),
        update_payload(article_id="not-numeric"),
        update_payload(article_id=True),
    ],
)
def test_article_id_matches_operation(payload):
    with patch.object(upload.requests, "request") as request:
        result = upload.main(event(payload), None)
    assert result["statusCode"] == 400
    request.assert_not_called()


@pytest.mark.parametrize(
    "returned",
    [
        article(state="published"),
        {**article(), "id": "not-numeric"},
        {**article(), "id": "١٢٣"},
        {**article(), "title": "Different title"},
        {**article(), "body_markdown": "Different body"},
        article(scheduled_publish_at=1780000000),
    ],
)
def test_create_requires_exact_safe_draft_readback(returned):
    with patch.object(upload.requests, "request", return_value=response(returned)):
        result = upload.main(event(create_payload()), None)
    assert result["statusCode"] == 502


def test_non_object_intercom_response_fails_closed():
    with patch.object(upload.requests, "request", return_value=response([])):
        result = upload.main(event(create_payload()), None)
    assert_reconciliation(result)


def test_create_timeout_is_non_retryable_and_requires_reconciliation():
    with patch.object(upload.requests, "request", side_effect=requests.Timeout()):
        result = upload.main(event(create_payload()), None)

    assert_reconciliation(result)


def test_post_update_readback_timeout_requires_reconciliation():
    old = article()
    updated = submitted_article()
    with patch.object(
        upload.requests,
        "request",
        side_effect=[response(old), response(updated), requests.Timeout()],
    ):
        result = upload.main(event(update_payload()), None)

    assert_reconciliation(result, "9001")


def test_known_upstream_4xx_is_not_reported_as_ambiguous_mutation():
    rejected = response({"type": "error.list"}, status=400)
    error = requests.HTTPError()
    error.response = rejected
    rejected.raise_for_status.side_effect = error
    with patch.object(upload.requests, "request", return_value=rejected):
        result = upload.main(event(create_payload()), None)

    assert result["statusCode"] == 400
    assert body(result) == {"ok": False, "error": "Intercom request failed"}


@pytest.mark.parametrize("status", [408, 418])
@pytest.mark.parametrize("operation", ["create", "update"])
def test_unclassified_mutation_4xx_requires_reconciliation(status, operation):
    rejected = response({"type": "error.list"}, status=status)
    error = requests.HTTPError()
    error.response = rejected
    rejected.raise_for_status.side_effect = error
    payload = create_payload() if operation == "create" else update_payload()
    side_effect = [rejected]
    expected_article_id = None
    if operation == "update":
        side_effect = [response(article()), rejected]
        expected_article_id = "9001"

    with patch.object(upload.requests, "request", side_effect=side_effect):
        result = upload.main(event(payload), None)

    assert_reconciliation(result, expected_article_id)


def test_expired_internal_deadline_stops_before_network_call():
    with patch.object(upload.requests, "request") as request:
        with pytest.raises(requests.Timeout):
            upload._request("GET", "/articles/9001", upload.time.monotonic() - 1)
    request.assert_not_called()


def test_rich_directive_must_survive_readback():
    payload = create_payload(
        body_markdown=':::callout backgroundColor="#feedaf80"\nBody\n:::'
    )
    returned = article(body_markdown="Ordinary body")
    with patch.object(upload.requests, "request", return_value=response(returned)):
        result = upload.main(event(payload), None)
    assert result["statusCode"] == 502


def test_published_draft_requires_pending_draft_confirmation():
    live = article(state="published")
    not_pending = submitted_article(state="published", pending=False)
    with patch.object(
        upload.requests,
        "request",
        side_effect=[
            response(live),
            response(live),
            response(not_pending),
            response(not_pending),
        ],
    ):
        result = upload.main(event(update_payload()), None)
    assert result["statusCode"] == 502


def test_unknown_existing_article_state_is_rejected_without_mutation():
    archived = article(state="archived")
    with patch.object(upload.requests, "request", return_value=response(archived)) as request:
        result = upload.main(event(update_payload()), None)
    assert result["statusCode"] == 409
    assert request.call_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "create", "title": "", "body_markdown": "body"},
        create_payload(operation_id="short"),
    ],
)
def test_bad_requests_are_rejected(payload):
    result = upload.main(event(payload), None)
    assert result["statusCode"] == 400


def test_malformed_base64_request_is_rejected_cleanly():
    result = upload.main(encoded_event("not+valid+base64"), None)
    assert result["statusCode"] == 400
    assert body(result)["error"] == "Request body must be valid base64-encoded JSON"


@pytest.mark.parametrize(
    "content",
    [
        {"title": "Broken \ud800 title"},
        {"description": "Broken \ud800 description"},
        {"body_markdown": "Broken \ud800 body"},
    ],
)
def test_invalid_utf8_content_fails_before_any_upstream_write(content):
    with patch.object(upload.requests, "request") as request:
        result = upload.main(event(create_payload(**content)), None)

    assert result["statusCode"] == 400
    assert body(result)["error"] == "Article content must be valid UTF-8 text"
    request.assert_not_called()


def test_wrong_secret_is_rejected_without_calling_intercom():
    with patch.object(upload.requests, "request") as request:
        result = upload.main(event(create_payload(), token="wrong"), None)
    assert result["statusCode"] == 401
    request.assert_not_called()


def test_short_server_secret_fails_closed(monkeypatch):
    monkeypatch.setenv("INTERCOM_DRAFT_BRIDGE_SECRET", "short")
    with patch.object(upload.requests, "request") as request:
        result = upload.main(event(create_payload()), None)
    assert result["statusCode"] == 500
    assert body(result)["error"] == "Draft bridge configuration error"
    request.assert_not_called()


@pytest.mark.parametrize("author_id", ["0", "-1", "١٢٣", "not-numeric"])
def test_invalid_author_id_fails_closed(monkeypatch, author_id):
    monkeypatch.setenv("INTERCOM_ARTICLE_AUTHOR_ID", author_id)
    with patch.object(upload.requests, "request") as request:
        result = upload.main(event(create_payload()), None)
    assert result["statusCode"] == 500
    assert body(result)["error"] == "Draft bridge configuration error"
    request.assert_not_called()


def test_only_post_is_supported():
    result = upload.main(event({}, method="GET"), None)
    assert result["statusCode"] == 405
