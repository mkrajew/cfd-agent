"""Run configuration: limits, default per-agent models, run IDs."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from cfdagent.config import RunConfig, RunLimits, make_run_id, slugify
from cfdagent.llm.config import LLMSettings

AGENTS = {"planner", "geometry", "mesh", "case_setup", "solver_runner", "analyst"}


def test_run_id_format_and_slug_sanitised():
    now = datetime(2026, 11, 12, 14, 30, 5)
    assert make_run_id("flow-case-1", now) == "20261112-143005-flow-case-1"
    run_id = make_run_id("  Weird/Slug_With  CAPS & Symbols!!  ", now)
    assert run_id == "20261112-143005-weird-slug-with-caps-symbols"
    long_id = make_run_id("a" * 100, now)
    assert long_id == "20261112-143005-" + "a" * 40
    # Truncation never leaves a trailing dash; nothing but [a-z0-9-] survives; empty falls back.
    assert not make_run_id("a" * 39 + "-b", now).endswith("-")
    assert make_run_id("../../etc/passwd", now) == "20261112-143005-etc-passwd"
    assert make_run_id("!!!", now) == "20261112-143005-run"
    assert make_run_id("\u00e9\u4e2d", now) == "20261112-143005-run"
    assert slugify("A  B") == "a-b"


def test_default_config_models_and_limits():
    config = RunConfig.default()
    assert set(config.models) == AGENTS
    for model in config.models.values():
        assert model.deployment == "gpt-oss-120b"
        assert model.reasoning_effort == "medium"
        assert model.max_tokens == 8192
    assert config.planner_structured_max_tokens == 4096
    limits = config.limits
    assert limits == RunLimits()
    assert limits.max_attempts_per_stage == 3
    assert limits.wall_clock_s == 7200
    assert limits.max_cost_usd == 1.0
    assert limits.max_ranks == 4
    assert limits.max_turns_per_attempt == 30
    assert limits.max_cells == 2_000_000
    assert limits.max_runtime_minutes == 90
    assert limits.max_memory_gb == 12
    assert config.sandbox.mode == "docker"
    assert config.benchmark_id is None
    assert config.task_set == "ad hoc"


def test_limits_reject_nonsense():
    for field, value in [
        ("max_ranks", 5),
        ("max_ranks", 0),
        ("max_attempts_per_stage", 0),
        ("max_cost_usd", -1),
        ("max_cost_usd", float("nan")),
        ("wall_clock_s", 0),
    ]:
        with pytest.raises(ValidationError):
            RunLimits(**{field: value})


def test_config_rejects_unknown_task_set_and_extra_fields():
    with pytest.raises(ValidationError):
        RunConfig(**{**RunConfig.default().model_dump(), "task_set": "mystery"})
    with pytest.raises(ValidationError):
        RunConfig(**{**RunConfig.default().model_dump(), "endpoint": "https://x"})


def test_llm_settings_from_env_use_a_single_retry(monkeypatch):
    monkeypatch.setenv("CFDAGENT_LLM_ENDPOINT", "https://example.invalid/openai/v1/")
    settings = RunConfig.llm_settings_from_env()
    assert isinstance(settings, LLMSettings)
    assert settings.max_retries == 1
    monkeypatch.delenv("CFDAGENT_LLM_ENDPOINT")
    with pytest.raises(ValueError):
        RunConfig.llm_settings_from_env()


def test_config_dump_holds_no_endpoint():
    assert "endpoint" not in RunConfig.default().model_dump_json().lower()
