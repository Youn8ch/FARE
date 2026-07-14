from __future__ import annotations

import re

from app.schemas import AclRawResponse, ExtractedFacts

NO_PATH_PATTERNS = (
    re.compile(r"未找到[^。\n]{0,80}(?:经过|途经)?[^。\n]{0,30}防火墙"),
    re.compile(r"不存在[^。\n]{0,80}(?:路径|途径)[^。\n]{0,30}防火墙"),
    re.compile(r"no\s+(?:firewall\s+)?path\s+(?:was\s+)?found", re.I),
    re.compile(r"no\s+firewall\s+(?:was\s+)?found", re.I),
)
FIREWALL_PATTERNS = (
    re.compile(r"(?:经过|途经)防火墙\s*[:：]?\s*([A-Za-z0-9_.-]+)"),
    re.compile(r"firewalls?\s*[:：]\s*([A-Za-z0-9_.-]+)", re.I),
)
ACL_PATTERN = re.compile(r"\baccess-list\s+([A-Za-z0-9_.-]+)", re.I)
OBJECT_PATTERN = re.compile(r"\bobject-group\s+([A-Za-z0-9_.-]+)", re.I)
PORT_PATTERN = re.compile(r"(?:端口|port)\s*[:：]?\s*(\d{1,5})\b", re.I)
AMBIGUOUS_PATTERN = re.compile(r"歧义|冲突|无法确定|ambiguous|conflict", re.I)


class AclFactExtractor:
    def extract(self, response: AclRawResponse) -> ExtractedFacts:
        text = "\n".join(part for part in (response.analysis, response.config) if part)
        no_path_evidence = [
            match.group(0)
            for pattern in NO_PATH_PATTERNS
            for match in pattern.finditer(text)
        ]
        firewalls = _unique(
            match.group(1) for pattern in FIREWALL_PATTERNS for match in pattern.finditer(text)
        )
        candidate_acls = _unique(match.group(1) for match in ACL_PATTERN.finditer(text))
        address_objects = _unique(match.group(1) for match in OBJECT_PATTERN.finditer(text))
        observed_ports = sorted(
            {
                int(match.group(1))
                for match in PORT_PATTERN.finditer(text)
                if int(match.group(1)) <= 65535
            }
        )
        evidence = [*no_path_evidence]
        evidence.extend(f"ACL 文本确认候选防火墙 {name}" for name in firewalls)
        return ExtractedFacts(
            firewalls=firewalls,
            explicit_no_path=bool(no_path_evidence),
            candidate_acls=candidate_acls,
            address_objects=address_objects,
            observed_ports=observed_ports,
            evidence=evidence,
            ambiguous=bool(AMBIGUOUS_PATTERN.search(text)),
        )


def _unique(values: object) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values))
