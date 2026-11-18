"""Longest-match Allow/Disallow evaluation, including wildcard paths and allow ties."""
import re
from urllib.parse import urlsplit


class RobotsPolicy:
    def __init__(self, text, agent):
        groups = []
        names, rules = [], []
        for raw in [*text.splitlines(), "User-agent: __end__"]:
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if rules:
                    groups.append((names, rules))
                    names, rules = [], []
                names.append(value.lower())
            elif key in {"allow", "disallow"} and names and value:
                rules.append((key == "allow", value))
        matching = []
        for names, rules in groups:
            scores = [0 if name == "*" else len(name) for name in names
                      if name == "*" or name in agent.lower()]
            if scores:
                matching.append((max(scores), rules))
        specificity = max((score for score, _ in matching), default=-1)
        self.rules = [rule for score, rules in matching if score == specificity for rule in rules]

    def can_fetch(self, url):
        parsed = urlsplit(url)
        path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
        matches = []
        for allow, rule in self.rules:
            end = rule.endswith("$")
            raw = rule[:-1] if end else rule
            pattern = "^" + re.escape(raw).replace(r"\*", ".*") + ("$" if end else "")
            if re.search(pattern, path):
                matches.append((len(raw.replace("*", "")), allow))
        return max(matches)[1] if matches else True
