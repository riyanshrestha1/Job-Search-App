from __future__ import annotations
import re

SUFFIXES = {
    "INC", "INCORPORATED", "LLC", "L L C", "LTD", "LIMITED", "CORP", "CORPORATION", "CO", "COMPANY",
    "LP", "LLP", "PLLC", "PC", "PA", "NA", "THE", "PLC", "GMBH", "USA", "US",
}
GENERIC_TAIL = {"TECHNOLOGIES", "TECHNOLOGY", "SOLUTIONS", "SYSTEMS", "GROUP", "SERVICES",
                "HOLDINGS", "AMERICA", "AMERICAS", "INTERNATIONAL", "GLOBAL", "CONSULTING",
                "SOFTWARE", "LABS", "ENTERPRISES", "PARTNERS"}


def normalize(name: str | None) -> str:
    if not name:
        return ""
    s = str(name).upper().replace("&", " AND ")
    s = re.split(r"\b(?:D\s?/?\s?B\s?/?\s?A|DBA|FKA|F/K/A)\b", s)[0]   # keep legal name
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    toks = [t for t in s.split() if t]
    while toks and toks[-1] in SUFFIXES:
        toks.pop()
    while toks and toks[0] == "THE":
        toks.pop(0)
    return " ".join(toks)


def strip_generic_tail(norm: str) -> str:
    toks = norm.split()
    while len(toks) > 1 and toks[-1] in GENERIC_TAIL:
        toks.pop()
    return " ".join(toks)
