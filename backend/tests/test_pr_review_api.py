from unittest.mock import MagicMock
from api.projects import get_latest_pr_review, list_pr_reviews

def test_latest_pr_no_documents():
    mock_firestore = MagicMock()
    mock_base = MagicMock()
    mock_firestore.collection.return_value.document.return_value = mock_base
    mock_base.collection.return_value.stream.return_value = []

    res = get_latest_pr_review("my-project", client=mock_firestore)
    assert res.has_pr is False
    assert res.requires_action is False

def test_latest_pr_unapproved():
    mock_firestore = MagicMock()
    doc1 = MagicMock()
    doc1.id = "10"
    doc1.to_dict.return_value = {
        "pr_number": 10,
        "head_sha": "abc1234",
        "persisted_at": "2026-11-18T05:00:00Z",
        "approved": None,
        "delivery_status": "pending",
        "messages": [{"id": "msg_1", "text": "hello"}]
    }

    mock_base = MagicMock()
    mock_firestore.collection.return_value.document.return_value = mock_base
    mock_base.collection.return_value.stream.return_value = [doc1]

    res = get_latest_pr_review("my-project", client=mock_firestore)
    assert res.has_pr is True
    assert res.pr_number == 10
    assert res.requires_action is True
    assert res.message_count == 1

def test_latest_pr_approved():
    mock_firestore = MagicMock()
    doc1 = MagicMock()
    doc1.id = "10"
    doc1.to_dict.return_value = {
        "pr_number": 10,
        "head_sha": "abc1234",
        "persisted_at": "2026-11-18T05:00:00Z",
        "approved": True,
        "delivery_status": "completed",
        "messages": [{"id": "msg_1", "text": "hello"}]
    }

    mock_base = MagicMock()
    mock_firestore.collection.return_value.document.return_value = mock_base
    mock_base.collection.return_value.stream.return_value = [doc1]

    res = get_latest_pr_review("my-project", client=mock_firestore)
    assert res.has_pr is True
    assert res.pr_number == 10
    assert res.requires_action is False

def test_list_prs():
    mock_firestore = MagicMock()
    doc1 = MagicMock()
    doc1.id = "10"
    doc1.to_dict.return_value = {
        "pr_number": 10,
        "head_sha": "abc1234",
        "persisted_at": "2026-11-18T05:00:00Z",
        "approved": False,
        "delivery_status": "pending",
        "messages": [{"id": "msg_1"}]
    }

    mock_base = MagicMock()
    mock_firestore.collection.return_value.document.return_value = mock_base
    mock_base.collection.return_value.stream.return_value = [doc1]

    res = list_pr_reviews("my-project", client=mock_firestore)
    assert len(res.prs) == 1
    assert res.prs[0].pr_number == 10
