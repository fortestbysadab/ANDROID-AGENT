import time
import unittest

from android_agent.agent.runtime import PendingApproval
from android_agent.approvals.store import InMemoryApprovalStore
from android_agent.models.base import ToolCall


class ApprovalStoreTests(unittest.TestCase):
    def make_pending(self):
        return PendingApproval(ToolCall("c1", "send_sms", {"number": "123", "message": "hi"}), "1", "confirm", "a" * 64)

    def test_approval_is_identity_bound_and_one_time(self):
        store = InMemoryApprovalStore(ttl_seconds=30)
        record = store.create(actor_id="42", chat_id=42, run_id="run", pending=self.make_pending())

        self.assertIsNone(store.consume(record.approval_id, actor_id="7", chat_id=42))
        self.assertIsNotNone(store.consume(record.approval_id, actor_id="42", chat_id=42))
        self.assertIsNone(store.consume(record.approval_id, actor_id="42", chat_id=42))

    def test_expired_approval_cannot_be_used(self):
        store = InMemoryApprovalStore(ttl_seconds=0)
        record = store.create(actor_id="42", chat_id=42, run_id="run", pending=self.make_pending())
        time.sleep(0.001)
        self.assertIsNone(store.consume(record.approval_id, actor_id="42", chat_id=42))


if __name__ == "__main__":
    unittest.main()
