"""
LSE Opportunity Scanner - RNS classification.

Deterministic, rule-based only - never an LLM inventing an event type.
The raw RNS headline/text remains the evidence; this only assigns a
CATEGORY label from keyword patterns, and separately whether that
category typically implies a POSITIVE, NEGATIVE, or AMBIGUOUS catalyst
- never conflating "a news story exists" with "this is good news",
per the explicit correction from earlier this session.

Categories and their catalyst direction are both stated explicitly and
testably below - not asserted by an AI in the moment.
"""
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class RNSClassification:
    category: str            # one of RNS_CATEGORIES, or "uncategorized"
    catalyst_direction: str  # "positive", "negative", "ambiguous", "neutral"
    matched_pattern: Optional[str]  # which rule fired, for debuggability


# Each category: (compiled regex, catalyst_direction). Checked in
# order - more specific/severe patterns are listed first so e.g.
# "profit warning" doesn't get caught by a looser "trading update"
# pattern first. Patterns are deliberately conservative (specific
# phrasing, not single ambiguous words) to avoid false classification.
RNS_RULES = [
    ("profit_warning", re.compile(r"\bprofit warning\b|\bbelow (?:market )?expectations\b|"
                                   r"\bdowngrad(?:e|ing) (?:its |our )?(?:full[- ]year )?guidance\b", re.I),
     "negative"),
    ("guidance_change_negative", re.compile(r"\breduc(?:e|ing|ed) (?:its |our )?guidance\b|"
                                             r"\blower(?:s|ed|ing) (?:its |our )?(?:full[- ]year )?outlook\b", re.I),
     "negative"),
    ("guidance_change_positive", re.compile(r"\brais(?:e|es|ing|ed) (?:its |our )?guidance\b|"
                                             r"\bupgrad(?:e|es|ing|ed) (?:its |our )?(?:full[- ]year )?outlook\b", re.I),
     "positive"),
    ("regulatory_issue", re.compile(r"\bregulatory (?:action|investigation|breach)\b|\bfine(?:d)? by\b|"
                                     r"\bsanction(?:s|ed)?\b", re.I), "negative"),
    ("contract_loss", re.compile(r"\bloses? (?:the )?contract\b|\bcontract terminat(?:ed|ion)\b", re.I),
     "negative"),
    ("contract_win", re.compile(r"\bcontract win\b|\bawarded (?:the |a )?contract\b|"
                                 r"\bnew contract\b", re.I), "positive"),
    ("acquisition", re.compile(r"\bacquir(?:e|es|ed|ing|es)\b.*\b(?:for|worth)\b|\bacquisition of\b", re.I),
     "ambiguous"),
    ("disposal", re.compile(r"\bdisposal of\b|\bsells? (?:its )?stake\b|\bdivest(?:s|ed|iture)\b", re.I),
     "ambiguous"),
    ("takeover", re.compile(r"\btakeover (?:bid|offer|approach)\b|\brecommended (?:cash )?offer\b", re.I),
     "ambiguous"),
    ("fundraising", re.compile(r"\bplacing (?:of|to raise)\b|\brights issue\b|\bequity raise\b", re.I),
     "ambiguous"),
    ("director_dealing", re.compile(r"\bdirector.?s?\s*dealing\b|\bpdmr\b|\bnotification of transactions? "
                                     r"(?:of|by) persons? discharging managerial responsibilit", re.I), "ambiguous"),
    ("director_appointment", re.compile(r"\bappoint(?:s|ed|ment) of (?:a )?(?:new )?(?:chief|director|ceo|cfo|"
                                         r"chairman)\b", re.I), "ambiguous"),
    ("director_resignation", re.compile(r"\bresignation of\b|\bsteps? down (?:as|from)\b|\bretir(?:e|es|ement)"
                                         r" of (?:the )?(?:chief|chairman|director)\b", re.I), "ambiguous"),
    ("dividend", re.compile(r"\bdividend (?:declaration|announcement)\b|\binterim dividend\b|"
                             r"\bfinal dividend\b", re.I), "positive"),
    ("buyback", re.compile(r"\bshare buy.?back\b|\brepurchase programme\b", re.I), "positive"),
    ("earnings_results", re.compile(r"\b(?:half|full)[- ]year results\b|\binterim results\b|"
                                     r"\bannual results\b|\bpreliminary results\b", re.I), "neutral"),
    ("trading_update", re.compile(r"\btrading (?:update|statement)\b|\bq[1-4] trading\b", re.I), "neutral"),
    ("strategic_update", re.compile(r"\bstrategic (?:update|review)\b|\bcapital markets day\b", re.I), "neutral"),
]

CATALYST_LABELS = {
    "positive": "🟢 likely positive catalyst",
    "negative": "🔴 likely negative catalyst",
    "ambiguous": "🟡 ambiguous - depends on terms/context, not assumed positive or negative",
    "neutral": "⚪ informational - direction depends on the actual content, not the announcement type alone",
}


def classify_rns(headline: str, body: str = "") -> RNSClassification:
    """
    Deterministic keyword classification of an RNS/company announcement
    headline (optionally with body text for additional matching
    context). Returns "uncategorized" honestly when nothing matches -
    never forces a headline into a category it doesn't genuinely fit.
    """
    text = f"{headline or ''} {body or ''}"
    for category, pattern, direction in RNS_RULES:
        match = pattern.search(text)
        if match:
            return RNSClassification(category=category, catalyst_direction=direction,
                                      matched_pattern=match.group(0))
    return RNSClassification(category="uncategorized", catalyst_direction="neutral", matched_pattern=None)
