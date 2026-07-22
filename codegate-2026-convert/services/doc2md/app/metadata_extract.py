"""Best-effort extraction of bid-notice metadata from converted body text.

Conservative by design: a WRONG value is worse than an empty one (it would
mislead the wiki), so each field is filled only on a high-confidence signal and
left absent otherwise. Whatever this returns is still overridable by an explicit
caller-supplied value.
"""

from __future__ import annotations

import re

# 조달청 e-발주 notice code, e.g. R26DC00229304 — a very strong, unambiguous signal.
_NARA_CODE = re.compile(r"\bR\d{2}[A-Z]{2}\d{6,}\b")

# 수요기관/발주기관 : <조직명>  (stop at line end, another ◇ bullet, or a table pipe)
_ORG = re.compile(
    r"(?:수요기관|발주기관|공고기관|발주처)\s*[:：]\s*([^\n◇○|]+)"
)

# 공고번호 : <값>  — only trusted when the captured value actually has a digit
# (blank form fields like "공고번호 제 호" must not produce a bogus number).
_NOTICE_NO = re.compile(
    r"공고번호\s*[:：]?\s*(제?\s*[0-9][0-9A-Za-z\-]*\s*호?)"
)


def extract(body: str, filename: str) -> dict[str, str]:
    found: dict[str, str] = {}

    official = _official_number(body, filename)
    if official:
        found["official_number"] = official

    org = _issuing_org(body)
    if org:
        found["issuing_org"] = org

    return found


def _official_number(body: str, filename: str) -> str | None:
    # 1) 조달청 code from filename (most reliable — it's how these files are named).
    m = _NARA_CODE.search(filename)
    if m:
        return m.group(0)
    # 2) same code anywhere in the body.
    m = _NARA_CODE.search(body)
    if m:
        return m.group(0)
    # 3) explicit 공고번호 field with a real (digit-bearing) value.
    m = _NOTICE_NO.search(body)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return None


def _issuing_org(body: str) -> str | None:
    m = _ORG.search(body)
    if not m:
        return None
    org = re.sub(r"\s+", " ", m.group(1)).strip(" .,·")
    # guard against grabbing a whole prose sentence ("수요기관의 장은 …")
    if 2 <= len(org) <= 40 and "은 " not in org and "는 " not in org:
        return org
    return None
