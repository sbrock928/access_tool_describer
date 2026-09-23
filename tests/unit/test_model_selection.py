import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from portfolio_analyzer.semantic import model_store
from portfolio_analyzer.semantic.config import (
    SemanticSettings,
    quick_mode_settings,
    select_model_preset,
    write_semantic_settings_template,
)
from portfolio_analyzer.semantic.model_store import (
    APPROVED_MODEL,
    QWEN_MODEL,
    ApprovedArtifact,
    acquire_approved_model,
    verify_model_directory,
)
from portfolio_analyzer.semantic.provider import LocalTransformersProvider


def test_model_preset_preserves_policy_and_clustering_and_switches_back(tmp_path: Path) -> None:
    path = tmp_path / "semantic.toml"
    write_semantic_settings_template(path)
    path.write_text(
        path.read_text().replace("retain_raw_prompts = false", "retain_raw_prompts = true")
    )
    settings = select_model_preset(path, "qwen")
    assert settings.model.repo_id == QWEN_MODEL.repo_id
    assert settings.model.revision == QWEN_MODEL.revision
    assert settings.model.local_path == tmp_path / "models" / QWEN_MODEL.local_identifier
    assert settings.profile.model_generation
    assert not settings.microsoft.model_generation
    assert settings.policy.retain_raw_prompts
    assert settings.clustering == SemanticSettings().clustering
    assert settings.execution.profile_output_tokens == 768
    assert quick_mode_settings(settings).execution.profile_output_tokens == 256
    assert select_model_preset(path, "granite").model.repo_id == APPROVED_MODEL.repo_id
    before = path.read_bytes()
    with pytest.raises(ValueError, match="qwen or granite"):
        select_model_preset(path, "unreviewed")
    assert path.read_bytes() == before


def test_qwen_unsharded_acquisition_verifies_selected_identity_and_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contents = {name: b"fixture" for name in QWEN_MODEL.expected_files}
    contents["config.json"] = json.dumps(
        {"model_type": "qwen2", "architectures": ["Qwen2ForCausalLM"]}
    ).encode()
    contents["tokenizer_config.json"] = b'{"tokenizer_class":"Qwen2Tokenizer"}'
    fixture = QWEN_MODEL.model_copy(
        update={
            "artifacts": tuple(
                ApprovedArtifact(
                    path=name, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
                )
                for name, data in contents.items()
            )
        }
    )
    monkeypatch.setattr(model_store, "QWEN_MODEL", fixture)

    def download(**kwargs: Any) -> str:
        assert kwargs["repo_id"] == QWEN_MODEL.repo_id
        assert kwargs["revision"] == QWEN_MODEL.revision
        assert set(kwargs["allow_patterns"]) == set(contents)
        assert kwargs["token"] is False
        destination = Path(kwargs["local_dir"])
        for name, data in contents.items():
            (destination / name).write_bytes(data)
        return str(destination)

    monkeypatch.setattr(
        model_store.importlib,
        "import_module",
        lambda _: SimpleNamespace(snapshot_download=download),
    )
    path = tmp_path / "qwen"
    manifest = acquire_approved_model(path, fixture)
    assert manifest.model_type == "qwen2"
    assert "model.safetensors.index.json" not in fixture.expected_files
    assert verify_model_directory(path).manifest == manifest
    settings = SemanticSettings.model_validate(
        {
            "model": {
                "repo_id": QWEN_MODEL.repo_id,
                "revision": QWEN_MODEL.revision,
                "local_path": path,
            }
        }
    )
    assert LocalTransformersProvider(settings).verified.manifest.repo_id == QWEN_MODEL.repo_id
    with pytest.raises(ValueError, match="configured model"):
        LocalTransformersProvider(SemanticSettings.model_validate({"model": {"local_path": path}}))
    (path / "model.safetensors").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="mismatch"):
        verify_model_directory(path)


def test_qwen_uses_reviewed_chatml_prompt_and_complete_input() -> None:
    provider = object.__new__(LocalTransformersProvider)
    provider.settings = SemanticSettings.model_validate(
        {
            "model": {
                "repo_id": QWEN_MODEL.repo_id,
                "revision": QWEN_MODEL.revision,
            }
        }
    )
    prompts: list[str] = []

    def tokenize(prompt: str, **_: Any) -> dict[str, Any]:
        prompts.append(prompt)
        return {"input_ids": SimpleNamespace(shape=(1, len(prompt)))}

    provider._tokenizer = tokenize
    provider._bounded_inputs(
        "Analyze",
        "<UNTRUSTED_SOURCE_DATA>data</UNTRUSTED_SOURCE_DATA>",
        4000,
        require_full_input=True,
    )
    assert prompts[0].startswith("<|im_start|>system\nAnalyze<|im_end|>")
    assert prompts[0].endswith("<|im_start|>assistant\n")
    assert "</UNTRUSTED_SOURCE_DATA>" in prompts[0]
    assert "<|start_of_role|>" not in prompts[0]
