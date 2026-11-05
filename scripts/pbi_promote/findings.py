"""A small error/warning collector shared by the validators."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Finding:
    level: str  # "error" or "warning"
    message: str
    path: str = ""

    def format(self) -> str:
        where = f" [{self.path}]" if self.path else ""
        return f"{self.level.upper()}: {self.message}{where}"

    def annotation(self) -> str:
        """GitHub Actions workflow command, so findings show up in the run summary and the PR."""
        cmd = "error" if self.level == "error" else "warning"
        msg = self.message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        if self.path:
            path = self.path.replace("%", "%25").replace(",", "%2C").replace(":", "%3A")
            return f"::{cmd} file={path}::{msg}"
        return f"::{cmd}::{msg}"


@dataclass
class Findings:
    items: list[Finding] = field(default_factory=list)

    def error(self, message: str, path: str = "") -> None:
        self.items.append(Finding("error", message, path))

    def warning(self, message: str, path: str = "") -> None:
        self.items.append(Finding("warning", message, path))

    def extend(self, other: "Findings") -> None:
        self.items.extend(other.items)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.items if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.items if f.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors
