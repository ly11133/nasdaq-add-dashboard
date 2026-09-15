"""Frozen scoring model metadata for Phase 1A.

This module does not recalculate or retune any score. It provides a stable
model identifier and fingerprints the existing row inputs/outputs so a later
replay can prove which rules were used.
"""

from __future__ import annotations

from pathlib import Path
import json

from data_contract import canonical_json, sha256_json


MODEL_PATH = Path(__file__).resolve().parent / "score_model.json"
CURRENT_SCORE_MODEL_VERSION = "NDX_SCORE_V2.0"


def load_model() -> dict:
    model = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    if model.get("model_version") != CURRENT_SCORE_MODEL_VERSION:
        raise ValueError("score_model.json 与当前模型版本不一致")
    material = {key: model[key] for key in ("model_version", "created_at", "component_config", "weights", "thresholds", "gate_rules", "eligibility_rules")}
    model["config_hash"] = sha256_json(material)
    return model


def model_config_hash(model: dict | None = None) -> str:
    return str((model or load_model())["config_hash"])


def score_input_fingerprint(model_version: str, inputs) -> str:
    return sha256_json({"model_version": model_version, "inputs": inputs})


def score_output_fingerprint(model_version: str, inputs, output) -> str:
    return sha256_json({"model_version": model_version, "inputs": inputs, "output": output})


__all__ = [
    "CURRENT_SCORE_MODEL_VERSION",
    "load_model",
    "model_config_hash",
    "score_input_fingerprint",
    "score_output_fingerprint",
]
