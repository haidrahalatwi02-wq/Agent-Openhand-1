"""Tests for the Agent Manager and its registry.

The manager is small, so these tests concentrate on the two properties that
actually matter to a multi-agent system: that a shared context really is shared,
and that one failing agent cannot destroy the work of the others. The rest is
registry bookkeeping that should fail loudly rather than silently.
"""

from __future__ import annotations

import json

import pytest

from lead_finder_agent.core import (
    AgentContext,
    AgentManager,
    AgentRunResult,
    BaseAgent,
    LeadFinderAgent,
)
from lead_finder_agent.search.providers.sample import SampleProvider
from lead_finder_agent.storage.sqlite_repository import SQLiteLeadRepository


class RecordingAgent(BaseAgent):
    """An agent that records how it was called, for asserting on routing."""

    name = "recording"
    description = "Records its calls"

    def __init__(self, context=None, result=None, name=None):
        super().__init__(context)
        self.calls = []
        self._result = result if result is not None else {"ok": True}
        if name is not None:
            self.name = name

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


class FailingAgent(BaseAgent):
    name = "failing"
    description = "Always raises"

    def run(self, **kwargs):
        raise RuntimeError("this agent is broken")


class TestRegistration:
    def test_register_returns_the_agent(self):
        manager = AgentManager()
        agent = RecordingAgent()
        assert manager.register(agent) is agent
        assert "recording" in manager
        assert manager.get("recording") is agent

    def test_names_are_sorted_and_stable(self):
        manager = AgentManager()
        for name in ("zeta", "alpha", "mid"):
            manager.register(RecordingAgent(name=name))
        assert manager.names() == ["alpha", "mid", "zeta"]

    def test_registration_is_case_insensitive(self):
        manager = AgentManager()
        manager.register(RecordingAgent())
        assert manager.get("RECORDING") is manager.get("recording")

    def test_a_duplicate_name_is_rejected(self):
        manager = AgentManager()
        manager.register(RecordingAgent())
        with pytest.raises(ValueError):
            manager.register(RecordingAgent())

    def test_a_duplicate_can_be_replaced_explicitly(self):
        manager = AgentManager()
        first = manager.register(RecordingAgent())
        second = manager.register(RecordingAgent(), replace=True)
        assert second is not first
        assert manager.get("recording") is second

    def test_an_agent_without_a_name_is_rejected(self):
        class Nameless(BaseAgent):
            name = ""

            def run(self, **kwargs):  # pragma: no cover - never reached
                return None

        with pytest.raises(ValueError):
            AgentManager().register(Nameless())

    def test_registering_a_non_agent_is_rejected(self):
        with pytest.raises(TypeError):
            AgentManager().register(object())  # type: ignore[arg-type]

    def test_an_unknown_agent_raises_with_the_available_names(self):
        manager = AgentManager()
        manager.register(RecordingAgent())
        with pytest.raises(KeyError) as excinfo:
            manager.get("nope")
        assert "recording" in str(excinfo.value)

    def test_unregister_removes_and_returns_the_agent(self):
        manager = AgentManager()
        agent = manager.register(RecordingAgent())
        assert manager.unregister("recording") is agent
        assert "recording" not in manager
        assert manager.unregister("recording") is None

    def test_len_and_iteration_follow_the_registry(self):
        manager = AgentManager()
        manager.register(RecordingAgent())
        assert len(manager) == 1
        assert [a.name for a in manager] == ["recording"]

    def test_info_describes_every_agent(self):
        manager = AgentManager()
        manager.register(RecordingAgent())
        assert manager.info() == [{"name": "recording", "description": "Records its calls"}]


class TestSharedContext:
    def test_every_agent_receives_the_managers_context(self):
        context = AgentContext()
        manager = AgentManager(context)
        first = manager.register(RecordingAgent(context))
        second = manager.register(FailingAgent(context))
        assert manager.context is context
        assert first.context is context
        assert second.context is context

    def test_a_manager_without_a_context_creates_one(self):
        manager = AgentManager()
        assert isinstance(manager.context, AgentContext)

    def test_agents_share_one_repository_through_the_context(self):
        # The whole point of the context: two agents read and write the same
        # store instead of each building its own.
        repo = SQLiteLeadRepository(":memory:")
        context = AgentContext(repository=repo)
        manager = AgentManager(context)
        first = manager.register(RecordingAgent(context))
        second = manager.register(FailingAgent(context))
        assert first.context.resolve_repository() is repo
        assert second.context.resolve_repository() is repo
        repo.close()


class TestRunningAgents:
    def test_run_passes_kwargs_to_the_agent(self):
        manager = AgentManager()
        agent = manager.register(RecordingAgent())
        assert manager.run("recording", city="Aden", limit=3) == {"ok": True}
        assert agent.calls == [{"city": "Aden", "limit": 3}]

    def test_run_returns_the_agents_result_unchanged(self):
        manager = AgentManager()
        manager.register(RecordingAgent(result=["a", "b"]))
        assert manager.run("recording") == ["a", "b"]

    def test_run_propagates_a_failure(self):
        manager = AgentManager()
        manager.register(FailingAgent())
        with pytest.raises(RuntimeError):
            manager.run("failing")

    def test_run_all_runs_every_registered_agent(self):
        manager = AgentManager()
        first = manager.register(RecordingAgent())
        manager.register(RecordingAgent(name="second"))
        results = manager.run_all(city="Aden")
        assert [r.agent for r in results] == ["recording", "second"]
        assert all(r.ok for r in results)
        assert first.calls == [{"city": "Aden"}]

    def test_run_all_can_target_a_subset_in_order(self):
        manager = AgentManager()
        manager.register(RecordingAgent())
        manager.register(RecordingAgent(name="second"))
        manager.register(RecordingAgent(name="third"))
        results = manager.run_all(["third", "recording"])
        assert [r.agent for r in results] == ["third", "recording"]

    def test_run_all_applies_shared_kwargs_to_every_agent(self):
        manager = AgentManager()
        agent = manager.register(RecordingAgent())
        manager.run_all(city="Aden", limit=5)
        assert agent.calls == [{"city": "Aden", "limit": 5}]

    def test_run_all_can_give_one_agent_its_own_kwargs(self):
        manager = AgentManager()
        agent = manager.register(RecordingAgent())
        manager.run_all(
            ["recording"], kwargs_by_agent={"recording": {"limit": 99}}, city="Aden"
        )
        assert agent.calls == [{"city": "Aden", "limit": 99}]


class TestFailureIsolation:
    """One broken agent must not discard the work of the others."""

    def test_run_isolated_reports_a_failure_instead_of_raising(self):
        manager = AgentManager()
        manager.register(FailingAgent())
        outcome = manager.run_isolated("failing")
        assert isinstance(outcome, AgentRunResult)
        assert outcome.ok is False
        assert outcome.agent == "failing"
        assert "broken" in outcome.error

    def test_run_isolated_reports_success(self):
        manager = AgentManager()
        manager.register(RecordingAgent(result=7))
        outcome = manager.run_isolated("recording")
        assert outcome.ok is True
        assert outcome.result == 7
        assert outcome.error is None

    def test_a_failing_agent_does_not_stop_the_others(self):
        manager = AgentManager()
        healthy = manager.register(RecordingAgent())
        manager.register(FailingAgent())
        results = {r.agent: r for r in manager.run_all()}

        assert results["failing"].ok is False
        assert results["recording"].ok is True
        # The healthy agent actually ran, rather than being skipped.
        assert healthy.calls

    def test_run_all_returns_one_result_per_agent(self):
        manager = AgentManager()
        manager.register(RecordingAgent())
        manager.register(FailingAgent())
        assert len(manager.run_all()) == 2

    def test_an_unknown_agent_is_reported_not_raised(self):
        manager = AgentManager()
        outcome = manager.run_isolated("does-not-exist")
        assert outcome.ok is False
        assert outcome.error

    def test_the_result_is_json_safe(self):
        manager = AgentManager()
        manager.register(FailingAgent())
        payload = manager.run_isolated("failing").to_dict()
        assert json.loads(json.dumps(payload))["ok"] is False


class TestDefaultAgents:
    def test_registering_defaults_registers_the_lead_finder(self):
        manager = AgentManager().register_default_agents()
        assert "lead_finder" in manager
        assert isinstance(manager.get("lead_finder"), LeadFinderAgent)

    def test_registering_defaults_twice_is_safe(self):
        manager = AgentManager().register_default_agents()
        manager.register_default_agents()
        assert len(manager) == 1

    def test_default_agents_share_the_managers_context(self):
        context = AgentContext()
        manager = AgentManager(context).register_default_agents()
        assert manager.get("lead_finder").context is context

    def test_the_default_agent_set_is_visible_on_the_cli(self):
        manager = AgentManager().register_default_agents()
        assert [entry["name"] for entry in manager.info()] == ["lead_finder"]


class TestLeadFinderThroughTheManager:
    """The manager routes to the real agent, offline, over a real repository."""

    def test_the_lead_finder_runs_through_the_manager(self, settings):
        repo = SQLiteLeadRepository(":memory:")
        context = AgentContext(
            settings=settings,
            repository=repo,
            providers=[SampleProvider()],
        )
        manager = AgentManager(context).register_default_agents()

        result = manager.run("lead_finder", city="Aden", limit=5, check_websites=False)

        assert result.leads
        assert repo.count() > 0
        repo.close()

    def test_a_multi_agent_run_survives_a_broken_agent(self, settings):
        repo = SQLiteLeadRepository(":memory:")
        context = AgentContext(
            settings=settings,
            repository=repo,
            providers=[SampleProvider()],
        )
        manager = AgentManager(context).register_default_agents()
        manager.register(FailingAgent())

        results = {r.agent: r for r in manager.run_all(city="Aden", limit=5, check_websites=False)}

        assert results["lead_finder"].ok is True
        assert results["lead_finder"].result.leads
        assert results["failing"].ok is False
        repo.close()
