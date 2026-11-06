from patchpilot.config import LLMSettings
from patchpilot.llm.required_models import main, required_ollama_models


def test_default_settings_name_each_local_model_once(monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_STAGE_MODELS"):
        monkeypatch.delenv(name, raising=False)

    settings = LLMSettings(_env_file=None)

    assert required_ollama_models(settings) == ("qwen2.5-coder:7b", "qwen3.5:4b")


def test_an_offline_provider_pulls_nothing():
    settings = LLMSettings(_env_file=None, llm_provider="fake", llm_stage_models="")

    assert required_ollama_models(settings) == ()


def test_a_shared_model_is_listed_once():
    settings = LLMSettings(
        _env_file=None,
        llm_provider="ollama",
        llm_model="shared:7b",
        llm_stage_models="",
    )

    assert required_ollama_models(settings) == ("shared:7b",)


def test_only_ollama_stages_are_selected():
    settings = LLMSettings(
        _env_file=None,
        llm_provider="fake",
        llm_model="unused:1b",
        llm_stage_models=(
            "triage=ollama:alpha:1b,"
            "investigate=ollama:beta:7b,"
            "plan=fake:unused:1b,"
            "develop=ollama:alpha:1b,"
            "review=openai:gpt-4.1-mini"
        ),
        llm_cloud_enabled=True,
        llm_cloud_base_url="https://api.openai.com/v1",
        llm_cloud_api_key="test-key",
        llm_cloud_capabilities="structured_output",
    )

    assert required_ollama_models(settings) == ("alpha:1b", "beta:7b")


def test_cli_prints_one_model_name_per_line(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "cli:1b")
    monkeypatch.setenv("LLM_STAGE_MODELS", "")
    monkeypatch.setenv("LLM_CLOUD_ENABLED", "false")

    assert main() == 0
    assert capsys.readouterr().out == "cli:1b\n"
