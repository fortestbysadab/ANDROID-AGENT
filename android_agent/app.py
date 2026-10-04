"""Composition root: build the whole agent once, in one place.

Before this, three entry points each assembled their own registry, runtime,
policy and authorisation callback. That is the kind of duplication that does
not announce itself: the copies stayed similar enough to look fine while
drifting on the details that matter, and a safety rule added to one would
silently miss the others.

Everything optional stays optional. No email settings means no email tools;
no Telegram token means the web console still starts.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from android_agent.agent.runtime import AgentRuntime
from android_agent.agent.session import SqliteSessionStore
from android_agent.approvals.store import InMemoryApprovalStore
from android_agent.channels import GmailChannel
from android_agent.config import Settings
from android_agent.documents import DocumentStore
from android_agent.models.openai_compatible import OpenAICompatiblePlanner
from android_agent.observability.audit import AuditSink, JsonlAuditSink
from android_agent.policy.engine import DefaultPolicy, PolicyDecision, UnattendedPolicy
from android_agent.schedule import ScheduleRunner, ScheduleStore, approval_hash_for
from android_agent.skills.loader import SkillRouter
from android_agent.tools.base import SchemaValidationError, ToolContext
from android_agent.tools.catalog import build_full_registry
from android_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_FALLBACK = (
    "You are an Android device assistant controlled by its owner. Select "
    "tools to carry out requests, state plainly what you did, and never "
    "claim an action succeeded unless the tool confirmed it."
)


@dataclass
class Application:
    """Everything the agent needs, wired together and ready to use."""

    settings: Settings
    registry: ToolRegistry
    runtime: AgentRuntime
    #: Same tools, a policy that refuses anything needing confirmation.
    #: Used for scheduled runs, where nobody is present to approve.
    unattended_runtime: AgentRuntime
    sessions: SqliteSessionStore
    approvals: InMemoryApprovalStore
    schedule_store: ScheduleStore
    schedule_runner: ScheduleRunner
    document_store: DocumentStore
    audit: AuditSink
    email_channel: Any | None = None

    @property
    def state_dir(self) -> str:
        return os.path.expanduser(self.settings.state_dir)

    def close(self) -> None:
        for resource in (self.sessions, self.schedule_store, self.document_store):
            try:
                resource.close()
            except Exception:
                logger.debug("Error closing %s", type(resource).__name__)


def build_application(
    settings: Settings,
    *,
    reporter: Callable[[Any], None] | None = None,
    system_prompt: str = SYSTEM_PROMPT_FALLBACK,
    planner=None,
) -> Application:
    """Assemble the agent.

    `reporter` receives finished scheduled tasks. Each front end delivers
    them differently - Telegram through the bot library, the one-shot tick
    straight over HTTPS - so the transport is injected rather than assumed.
    `planner` is injectable for tests and for the Needle fast path.
    """
    state_dir = os.path.expanduser(settings.state_dir)
    os.makedirs(state_dir, exist_ok=True)
    audit = JsonlAuditSink(os.path.join(state_dir, "audit.jsonl"))
    owner_policy = DefaultPolicy(str(settings.owner_chat_id))

    schedule_store = ScheduleStore(os.path.join(state_dir, "schedule.db"))
    registry: ToolRegistry | None = None

    def needs_authorisation(tool_name, arguments, *, force=False):
        """Hash this exact action, or None if the owner must authorise first.

        Asks the real policy rather than keeping a second list of what is
        risky, so the two can never disagree.
        """
        tool = registry.get(tool_name) if registry else None
        if tool is None:
            return None
        context = ToolContext(
            str(settings.owner_chat_id), int(settings.owner_chat_id),
            "schedule-authorisation", direct_user_request=False,
        )
        try:
            validated = tool.validate(arguments)
        except SchemaValidationError:
            return None
        if owner_policy.evaluate(context, tool, validated).decision is not PolicyDecision.ALLOW:
            if not force:
                return None
        return approval_hash_for(registry, tool_name, validated)

    email_channel = None
    if settings.email_enabled:
        email_channel = GmailChannel(
            settings.email_address,
            settings.email_app_password,
            imap_host=settings.email_imap_host,
            imap_port=settings.email_imap_port,
            smtp_host=settings.email_smtp_host,
            smtp_port=settings.email_smtp_port,
            display_name=settings.email_display_name,
        )
        logger.info("Email connector enabled for %s", settings.email_address)

    document_store = DocumentStore(os.path.join(state_dir, "documents.db"))

    registry = build_full_registry(
        schedule_store,
        needs_authorisation=needs_authorisation,
        email_channel=email_channel,
        document_store=document_store,
        search_settings=settings if settings.search_enabled else None,
    )
    if settings.search_enabled:
        logger.info("Web search enabled via %s", settings.search_provider)

    if planner is None:
        planner = OpenAICompatiblePlanner(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            dialect=settings.llm_dialect,
            timeout_seconds=settings.request_timeout_seconds,
        )

    skills = SkillRouter.bundled()
    runtime = AgentRuntime(
        planner=planner, registry=registry, policy=owner_policy,
        system_prompt=system_prompt, audit=audit, skill_router=skills,
    )
    unattended_runtime = AgentRuntime(
        planner=planner, registry=registry, policy=UnattendedPolicy(owner_policy),
        system_prompt=system_prompt, audit=audit, skill_router=skills,
    )

    sessions = SqliteSessionStore(
        os.path.join(state_dir, "sessions.db"),
        ttl_seconds=settings.session_ttl_seconds,
        max_messages=settings.session_max_messages,
    )

    def log_only(run) -> None:
        logger.info("Scheduled task %s: %s", run.task_id, run.message)

    schedule_runner = ScheduleRunner(
        store=schedule_store,
        runtime=runtime,
        unattended_runtime=unattended_runtime,
        owner_id=str(settings.owner_chat_id),
        chat_id=int(settings.owner_chat_id),
        reporter=reporter or log_only,
        audit=audit,
    )

    return Application(
        settings=settings,
        registry=registry,
        runtime=runtime,
        unattended_runtime=unattended_runtime,
        sessions=sessions,
        approvals=InMemoryApprovalStore(ttl_seconds=300),
        schedule_store=schedule_store,
        schedule_runner=schedule_runner,
        document_store=document_store,
        audit=audit,
        email_channel=email_channel,
    )
