"""Minimal robots.txt parser with RFC 9309 longest-pattern-wins precedence.

Python's stdlib urllib.robotparser scores competing rules by the length of
the *matched span in the URL* rather than the length of the *declared
pattern*. That misranks a rule like "Allow: /*?page=" against a catch-all
"Disallow: *?*": the catch-all's trailing wildcard greedily matches to the
end of the URL, giving it a longer match span than the specific carve-out
it was written to override — even though the site's own robots.txt clearly
intends /*?page= to win. Google's documented algorithm (and RFC 9309) score
by the length of the rule entry itself, which is what this implements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    parts = body.split("*")
    escaped = ".*".join(re.escape(p) for p in parts)
    if anchored_end:
        escaped += "$"
    return re.compile(escaped)


@dataclass
class RobotsRule:
    path: str
    allow: bool

    def __post_init__(self) -> None:
        self._regex = _pattern_to_regex(self.path)

    def matches(self, url_path: str) -> bool:
        return self._regex.match(url_path) is not None


class RobotsPolicy:
    def __init__(self, rules: list[RobotsRule], deny_all: bool = False):
        self.rules = rules
        self.deny_all = deny_all

    @classmethod
    def deny_everything(cls) -> "RobotsPolicy":
        """Used when robots.txt couldn't be fetched/verified — fail closed
        rather than assume scraping is allowed."""
        return cls(rules=[], deny_all=True)

    def can_fetch(self, url: str) -> bool:
        if self.deny_all:
            return False
        parsed = urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if not path:
            path = "/"
        best_len = -1
        allow = True
        for rule in self.rules:
            if rule.matches(path) and len(rule.path) > best_len:
                best_len = len(rule.path)
                allow = rule.allow
        return allow


def parse_robots(text: str, user_agent: str) -> RobotsPolicy:
    blocks: list[tuple[list[str], list[RobotsRule]]] = []
    current_agents: list[str] = []
    current_rules: list[RobotsRule] = []
    started_rules = False

    def flush() -> None:
        nonlocal current_agents, current_rules, started_rules
        if current_agents:
            blocks.append((current_agents, current_rules))
        current_agents, current_rules, started_rules = [], [], False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if started_rules:
                flush()
            current_agents.append(value)
        elif key == "allow":
            started_rules = True
            if value:
                current_rules.append(RobotsRule(value, True))
        elif key == "disallow":
            started_rules = True
            if value:
                current_rules.append(RobotsRule(value, False))
    flush()

    matched_rules: list[RobotsRule] | None = None
    wildcard_rules: list[RobotsRule] | None = None
    ua_lower = user_agent.lower()
    for agents, rules in blocks:
        for agent in agents:
            if agent == "*":
                wildcard_rules = rules
            elif agent.lower() in ua_lower:
                matched_rules = rules

    return RobotsPolicy(matched_rules if matched_rules is not None else (wildcard_rules or []))
