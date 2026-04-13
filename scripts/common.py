from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI


ROOT_DIR = Path(__file__).resolve().parents[1]
GENERATED_DIR = ROOT_DIR / "generated"
TMP_DIR = ROOT_DIR / "tmp"


def bootstrap_env() -> None:
    """Load environment variables from .env when running locally."""
    load_dotenv(ROOT_DIR / ".env")


def load_settings() -> dict[str, Any]:
    settings_path = ROOT_DIR / "config" / "settings.json"
    return json.loads(settings_path.read_text(encoding="utf-8"))


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if not isinstance(value, str):
        return default
    stripped = value.strip()
    return stripped if stripped else default


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_text(path: Path, content: str) -> None:
    ensure_directory(path.parent)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def load_prompt(name: str) -> str:
    prompt_path = ROOT_DIR / "prompts" / name
    return prompt_path.read_text(encoding="utf-8")


def strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1]).strip()
    return cleaned


def get_openai_client() -> OpenAI:
    api_key = get_required_env("OPENAI_API_KEY")
    return OpenAI(api_key=api_key)


def get_model(settings: dict[str, Any]) -> str:
    return optional_env("OPENAI_MODEL", settings.get("openai_model", "gpt-5.4-mini"))


def generate_json_payload(prompt: str, settings: dict[str, Any]) -> dict[str, Any]:
    """
    Generate JSON content with the Responses API.
    We ask for strict JSON and do a small cleanup pass before parsing.
    """
    client = get_openai_client()
    response = client.responses.create(
        model=get_model(settings),
        input=prompt,
    )
    content = strip_code_fence(response.output_text)
    return json.loads(content)


def run_command(command: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def copy_tree_contents(source: Path, destination: Path) -> None:
    ensure_directory(destination)
    for item in source.iterdir():
        target = destination / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
