# -*- coding: utf-8 -*-
import re

# Final product taxonomy
PRODUCT_LINES = [
    "Flight",
    "Rack Conveyor",
    "Door Type",
    "Undercounter",
    "Pot & Pan",
    "Other",
]

# --- HIGH-LEVEL MAPPING RULES ---
# Precedence matters: first matching group wins.
# Your requests applied:
#  - Any EUCC / EUCCW -> Flight (highest precedence)
#  - Glasswashers (CG..., "Glasswasher") -> Undercounter
#  - Keep Rack Conveyor via explicit terms + PRO-number families
#  - Keep Door Type via DH/DL/Hood keywords
#  - Keep Undercounter via UH/UL/UHM/UHB/UCC/UCCW (EXCEPT when Flight already matched)
#  - Pot & Pan via PP*, "Pot Pan", P524 (as seen in your logs)

PATTERNS = {
    "Flight": [
        re.compile(r"\b(EUCCW?|EUCC)\b", re.I),            # override: EUCC/EUCCW -> Flight
        re.compile(r"\bflight\b", re.I),
        re.compile(r"\bPRO\s*Flight\b", re.I),
        re.compile(r"\bE\s*Series\s*Flight\b", re.I),
        re.compile(r"\bFlight Machine\b", re.I),
        re.compile(r"\bHeat Recovery Flight\b", re.I),
    ],
    "Rack Conveyor": [
        re.compile(r"\brack\s*conveyor(s)?\b", re.I),
        re.compile(r"\b(loader|unloader)\b", re.I),
        re.compile(r"\b(44|54|64|66|76|80|86|90)\s*PRO\b", re.I),  # PRO families
        re.compile(r"\bPRO\s*90B\b", re.I),
    ],
    "Door Type": [
        re.compile(r"\b(DH|DL)\s*\d", re.I),
        re.compile(r"\bHood Type\b", re.I),
        re.compile(r"\bTall Hood\b", re.I),
        re.compile(r"\bDoor Type\b", re.I),
    ],
    "Undercounter": [
        # Glasswashers belong to Undercounter (your directive)
        re.compile(r"\bGlasswasher(s)?\b", re.I),
        re.compile(r"\bCG[0-9 ]*\b", re.I),
        # Undercounter families
        re.compile(r"\bU(H|L|HM|HB)\s*\d{2,4}[A-Z]?\b", re.I),  # UH200, UL130, UHM4, UHB, etc.
        re.compile(r"\bUCC(W)?\b", re.I),
        re.compile(r"\bUndercounter\b", re.I),
    ],
    "Pot & Pan": [
        re.compile(r"\bPP\b", re.I),
        re.compile(r"\bPP\s*\d+\b", re.I),
        re.compile(r"\bPot\s*&?\s*Pan\b", re.I),
        re.compile(r"\bP524\b", re.I),
    ],
    # "Other" is default
}

DOC_TYPES = [
    ("install", re.compile(r"\binstall(ation)?\b", re.I)),
    ("operations", re.compile(r"\b(ops|operation(s)?)\b", re.I)),
    ("cleaning", re.compile(r"\bclean(ing)?\b", re.I)),
    ("service", re.compile(r"\bservice\b", re.I)),
    ("parts", re.compile(r"\bparts?\b", re.I)),
    ("manual", re.compile(r"\bmanual\b", re.I)),
]

LANG_FLAGS = [("spanish", re.compile(r"\bspanish\b|\bespañol\b", re.I))]
