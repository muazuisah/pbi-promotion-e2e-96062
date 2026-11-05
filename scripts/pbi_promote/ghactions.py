"""Helpers for writing GitHub Actions step outputs and job summaries."""

from __future__ import annotations

import os
import uuid


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"[output] {name}={value}")
        return
    delimiter = f"EOF_{uuid.uuid4().hex}"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def add_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        print(markdown)
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(markdown + "\n")


def in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"
