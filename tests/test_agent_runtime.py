import unittest

from android_agent.agent.runtime import AgentRuntime, RunStatus, RuntimeLimits
from android_agent.models.base import PlannerResponse, ToolCall
from android_agent.observability.audit import MemoryAuditSink
from android_agent.policy.engine import DefaultPolicy
from android_agent.tools.base import Risk, ToolResult, ToolSpec
from android_agent.tools.registry import ToolRegistry

SCHEMA = {
    "type": "object",
    "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 15}},
    "required": ["level"],
    "additionalProperties": False,
}


class FakePlanner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((list(messages), list(tools)))
        return self.responses.pop(0)


def make_tool(risk=Risk.REVERSIBLE, calls=None):
    def handler(context, arguments):
        if calls is not None:
            calls.append((context, arguments))
        return ToolResult.ok("volume changed", {"level": arguments["level"]})

    return ToolSpec(
        name="set_volume",
        description="Set the Android media volume to a bounded raw level.",
        input_schema=SCHEMA,
        risk=risk,
        handler=handler,
        timeout_seconds=1,
    )


class RuntimeTests(unittest.TestCase):
    def test_executes_valid_allowed_call_and_returns_observation(self):
        calls = []
        planner = FakePlanner(
            PlannerResponse(tool_calls=(ToolCall("c1", "set_volume", {"level": 8}),)),
            PlannerResponse(text="Volume is now 8."),
        )
        audit = MemoryAuditSink()
        runtime = AgentRuntime(
            planner=planner,
            registry=ToolRegistry([make_tool(calls=calls)]),
            policy=DefaultPolicy("42"),
            system_prompt="Use tools when needed.",
            audit=audit,
        )

        outcome = runtime.run("set volume to 8", actor_id="42", chat_id=42)

        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(outcome.text, "Volume is now 8.")
        self.assertEqual(calls[0][1], {"level": 8})
        self.assertEqual(outcome.tool_results[0].status, "ok")
        self.assertTrue(any(event["event"] == "policy.allow" for event in audit.events))
        self.assertEqual(planner.requests[1][0][-1]["role"], "tool")

    def test_invalid_arguments_never_reach_handler(self):
        calls = []
        planner = FakePlanner(
            PlannerResponse(tool_calls=(ToolCall("c1", "set_volume", {"level": 99}),)),
            PlannerResponse(text="I could not set that level."),
        )
        runtime = AgentRuntime(
            planner=planner,
            registry=ToolRegistry([make_tool(calls=calls)]),
            policy=DefaultPolicy("42"),
            system_prompt="Use tools.",
        )

        outcome = runtime.run("set it impossibly high", actor_id="42", chat_id=42)

        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertFalse(calls)
        self.assertEqual(outcome.tool_results[0].error_code, "invalid_arguments")

    def test_external_action_pauses_for_approval(self):
        planner = FakePlanner(
            PlannerResponse(tool_calls=(ToolCall("c1", "set_volume", {"level": 8}),)),
        )
        runtime = AgentRuntime(
            planner=planner,
            registry=ToolRegistry([make_tool(risk=Risk.EXTERNAL_SIDE_EFFECT)]),
            policy=DefaultPolicy("42"),
            system_prompt="Use tools.",
        )

        outcome = runtime.run("send it", actor_id="42", chat_id=42)

        self.assertEqual(outcome.status, RunStatus.APPROVAL_REQUIRED)
        self.assertEqual(outcome.pending_approvals[0].call.arguments, {"level": 8})
        self.assertEqual(len(outcome.pending_approvals[0].argument_hash), 64)

        result = runtime.execute_approved(
            outcome.pending_approvals[0], actor_id="42", chat_id=42, run_id=outcome.run_id
        )
        self.assertEqual(result.status, "ok")

    def test_non_owner_is_denied(self):
        calls = []
        planner = FakePlanner(
            PlannerResponse(tool_calls=(ToolCall("c1", "set_volume", {"level": 8}),)),
            PlannerResponse(text="Denied."),
        )
        runtime = AgentRuntime(
            planner=planner,
            registry=ToolRegistry([make_tool(calls=calls)]),
            policy=DefaultPolicy("42"),
            system_prompt="Use tools.",
        )

        outcome = runtime.run("set volume", actor_id="7", chat_id=7)

        self.assertFalse(calls)
        self.assertEqual(outcome.tool_results[0].status, "denied")

    def test_repeated_call_circuit_breaker(self):
        repeated = PlannerResponse(tool_calls=(ToolCall("c1", "set_volume", {"level": 8}),))
        planner = FakePlanner(repeated, repeated, repeated)
        runtime = AgentRuntime(
            planner=planner,
            registry=ToolRegistry([make_tool()]),
            policy=DefaultPolicy("42"),
            system_prompt="Use tools.",
            limits=RuntimeLimits(max_model_turns=8, max_tool_calls=12, max_same_call=2),
        )

        outcome = runtime.run("loop", actor_id="42", chat_id=42)

        self.assertEqual(outcome.status, RunStatus.BUDGET_EXHAUSTED)
        self.assertIn("circuit breaker", outcome.text)


if __name__ == "__main__":
    unittest.main()
