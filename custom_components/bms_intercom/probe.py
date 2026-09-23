"""Self-diagnostics: probe the panel and write a readable, secret-free report.

The owner presses «Проверить панель» (or calls the `bms_intercom.probe`
service); the report goes to the Home Assistant log and into the button's
attributes. That is how we find out which ISAPI shapes this particular
firmware supports — without anyone handing over the panel password.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .endpoints import redact, snippet, status_ok

REPORT_MAX_ATTR_LEN = 14000  # HA state attributes must stay reasonably small


@dataclass
class ProbeResult:
    """One probed endpoint."""

    name: str
    method: str
    url: str
    status: int | None = None
    content_type: str = ""
    body: str = ""
    auth: str = ""
    #: Verbatim `WWW-Authenticate` of a 401 — carries no secret, never redacted.
    challenge: str = ""
    #: What we put in `Authorization` (digest verbatim, basic reduced to the
    #: scheme: its base64 payload *is* the password).
    sent_auth: str = ""
    error: str = ""
    secrets: tuple[str | None, ...] = field(default=(), repr=False)

    @property
    def ok(self) -> bool:
        return status_ok(self.status)

    def as_line(self) -> str:
        url = redact(self.url, *self.secrets)
        status = self.status if self.status is not None else "—"
        parts = [f"{self.method:<4} {url}", f"-> {status}"]
        if self.auth:
            parts.append(f"auth={self.auth}")
        if self.content_type:
            parts.append(self.content_type.split(";")[0])
        if self.error:
            parts.append(f"ошибка: {redact(self.error, *self.secrets)}")
        body = snippet(self.body, secrets=self.secrets)
        if body:
            parts.append(f"| {body}")
        line = "  ".join(parts)
        if self.challenge:
            line += f"\n        challenge: {self.challenge}"
        if self.sent_auth:
            line += f"\n        отправили: {self.sent_auth}"
        return line

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "method": self.method,
            "url": redact(self.url, *self.secrets),
            "status": self.status,
            "ok": self.ok,
            "auth": self.auth,
            "content_type": self.content_type,
            "body": snippet(self.body, secrets=self.secrets),
            "challenge": self.challenge,
            "sent_auth": self.sent_auth,
            "error": redact(self.error, *self.secrets),
        }


def format_probe_report(
    results: list[ProbeResult],
    *,
    host: str = "",
    summary: dict[str, Any] | None = None,
) -> str:
    """Human-readable report for the HA log (and the entity attributes)."""
    lines = [f"BMS Intercom — проверка панели {host}".rstrip()]
    for item in summary.items() if summary else ():
        key, value = item
        lines.append(f"  {key}: {value}")
    lines.append("  " + "-" * 60)
    for index, result in enumerate(results, 1):
        mark = "OK  " if result.ok else "FAIL"
        lines.append(f"  {index:>2}. [{mark}] {result.name}: {result.as_line()}")
    if not results:
        lines.append("  (ни один запрос не выполнен)")
    return "\n".join(lines)


def report_attributes(
    results: list[ProbeResult],
    *,
    host: str = "",
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attributes for the probe button entity."""
    text = format_probe_report(results, host=host, summary=summary)
    if len(text) > REPORT_MAX_ATTR_LEN:
        text = text[:REPORT_MAX_ATTR_LEN] + "\n  … отчёт обрезан, см. журнал HA"
    attrs: dict[str, Any] = {
        "probe_report": text,
        "probe_results": [r.as_dict() for r in results],
    }
    if summary:
        attrs["probe_summary"] = {k: v for k, v in summary.items()}
    return attrs
