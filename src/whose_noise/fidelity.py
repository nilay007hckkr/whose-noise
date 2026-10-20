"""Deterministic, explainable simulator-fidelity checks on tau2 transcripts (no LLM calls).

Every check reads only the ``messages`` of a simulation in ``results.json`` (plus the run
``info`` for the text input style). Each flag comes with the text cue that triggered it,
so a reader can verify a flag by eye. The checks are screens, not verdicts: they find
candidate simulator errors cheaply; a sample still has to be read by hand.

Per simulation (see :data:`FIDELITY_COLUMNS`):

``customer_turns`` / ``agent_turns``
    Messages with non-empty text from the user simulator / the agent. ``agent_turns``
    leaves out tool-call-only assistant messages, so it differs from the loader's
    ``n_agent_turns`` (which counts every assistant message).

``end_token`` / ``stop_with_content``
    Which special token (``STOP``, ``TRANSFER``, ``OUT-OF-SCOPE``) the last token-bearing
    customer message carries, and whether a ``###STOP###`` message also carries other
    text (e.g. ``"Yes.###STOP###"``).

``premature_stop`` (strict)
    A customer message contains ``###STOP###`` **and** other text that agrees to a
    pending request (``affirmation_cue``: "yes", "go ahead", "haan", "जी, ठीक है", ...;
    not when negated or withdrawn: "please do not cancel", "I'd rather not go ahead",
    "Okay, actually no, don't cancel", "cancel nahi karna"), **and** the agent's
    immediately preceding text message asked for confirmation (``confirmation_cue``:
    "would you like me to", "shall I", 'reply "yes"', "क्या मैं ... कर दूँ?", or "confirm",
    "proceed", "पुष्टि" in a sentence with request syntax such as "please", "?" or
    "कृपया"; statements, list items and headers do not count), **and** that request is
    about a write still pending: either no write tool call happened before it, or a
    sentence carrying the cue names a write ("Shall I also cancel order #W1?"; offers
    after the work was done, such as "Would you like me to generate that link?", do not
    count), **and** no write tool call happened from that agent message to the end of
    the conversation. The conversation ends on the STOP, so the agent never gets to
    execute what the customer just agreed to. This is the "Yes.###STOP###" failure found
    in the RQ1 chunk 1 audit (gpt-oss-120b task 18 trial 0, task 9 trial 1, task 32
    trial 0).

``stop_after_unexecuted_confirmation`` (loose)
    The same without the affirmation requirement: any ``###STOP###`` right after an
    agent confirmation request about a pending write that was never executed, including
    a bare ``###STOP###`` and an explicit "no". It is an upper-bound screen: a customer
    who declines and leaves is legitimate, so read the flagged transcripts.

    Recall must match across languages before English and Hindi rates are compared: the
    Hindi and romanized-Hindi cues mirror the English ones (tests/test_fidelity.py), and
    the persona's habitual "जी," / "ji," opener is skipped before the start-of-message
    checks.

``transfer_requested_by_customer``
    A customer message asks for a human / live agent / supervisor or to be transferred
    (English and Hindi cues; the ``###TRANSFER###`` token itself is not a request).
    ``agent_transferred`` records whether the agent called ``transfer_to_human_agents``.
    Some tasks script the request, so this is descriptive, not an error flag.

Script statistics (customer turns only)
    Special tokens are replaced by a space (a word stuck to ``###STOP###`` still counts),
    then tokens containing a digit, ``@``, ``#`` or ``_`` are skipped (order ids such as
    ``#W2378156``, emails, user ids, prices). A "letter" is a
    character of Unicode category L* or M* (marks are counted so that Devanagari vowel
    signs count as script). Devanagari = block U+0900-U+097F; Latin = characters whose
    Unicode name starts with ``LATIN``. Brand and product words (e.g. "Google Home")
    are *not* excluded, so the Latin share of a native-script run is never 0.

``script_leakage``
    Uses ``info.text_input_style`` (recorded by the ``tau-multilingual`` branch; None
    means ``native_script`` there) and the user's language (from the simulation's
    ``speech_environment.language``, ``info.user_persona_id``, ``info.task_set_name`` or a
    ``_<lang>`` task-id suffix). For a Devanagari-script language
    (Hindi etc.): ``romanized`` / ``code_mixed`` leak when any customer turn contains a
    Devanagari letter; ``native_script`` leaks when more than
    ``native_latin_share_max`` (default 0.5) of the letters are Latin, i.e. the user typed
    mostly romanized. Otherwise (English runs, non-Devanagari languages) the flag is NA.

Use :func:`add_fidelity` to join these columns onto a frame from
:func:`whose_noise.tau2_results.load_runs`, :func:`fidelity_summary` for per-run rates by
outcome, and :func:`format_fidelity` for the text block printed by ``analyze --fidelity``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from whose_noise.tau2_results import read_results, resolve_results_path

STOP_TOKEN = "###STOP###"
TRANSFER_TOKEN = "###TRANSFER###"
OUT_OF_SCOPE_TOKEN = "###OUT-OF-SCOPE###"
SPECIAL_TOKENS: dict[str, str] = {
    STOP_TOKEN: "STOP",
    TRANSFER_TOKEN: "TRANSFER",
    OUT_OF_SCOPE_TOKEN: "OUT-OF-SCOPE",
}

# Agent tools tagged ToolType.WRITE in tau2 (v1.0.1 and tau-multilingual @8ff48b8):
# src/tau2/domains/{retail,airline,telecom}/tools.py. Gold write actions found in a
# simulation's reward_info.action_checks are added per simulation.
DEFAULT_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        # retail
        "cancel_pending_order",
        "exchange_delivered_order_items",
        "modify_pending_order_address",
        "modify_pending_order_items",
        "modify_pending_order_payment",
        "modify_user_address",
        "return_delivered_order_items",
        # airline
        "book_reservation",
        "cancel_reservation",
        "send_certificate",
        "update_reservation_baggages",
        "update_reservation_flights",
        "update_reservation_passengers",
        # telecom (agent side)
        "suspend_line",
        "resume_line",
        "send_payment_request",
        "enable_roaming",
        "disable_roaming",
        "refuel_data",
    }
)
TRANSFER_TOOL = "transfer_to_human_agents"

DEVANAGARI_LANGUAGES: frozenset[str] = frozenset({"hi", "mr", "ne", "sa"})
ROMAN_STYLES: frozenset[str] = frozenset({"romanized", "code_mixed"})
NATIVE_LATIN_SHARE_MAX = 0.5
_LANGUAGE_NAMES = {"hindi": "hi", "english": "en", "marathi": "mr", "nepali": "ne"}

# Boolean flags summarised per run by fidelity_summary().
FLAG_COLUMNS: tuple[str, ...] = (
    "premature_stop",
    "stop_after_unexecuted_confirmation",
    "transfer_requested_by_customer",
)

FIDELITY_COLUMNS: tuple[str, ...] = (
    "customer_turns",
    "agent_turns",
    "end_token",
    "stop_with_content",
    "premature_stop",
    "stop_after_unexecuted_confirmation",
    "confirmation_cue",
    "affirmation_cue",
    "stop_message",
    "transfer_requested_by_customer",
    "customer_transfer_turns",
    "transfer_cue",
    "agent_transferred",
    "text_input_style",
    "user_language",
    "leak_script",
    "customer_letters",
    "devanagari_letters",
    "latin_letters",
    "devanagari_share",
    "latin_share",
    "customer_turns_with_devanagari",
    "customer_turns_with_latin",
    "script_leak_share",
    "script_leak_turns",
    "script_leakage",
)
KEY_COLUMNS: tuple[str, ...] = ("results_path", "run_name", "task_id", "trial")
_OCC = "_occurrence"

# --------------------------------------------------------------------------- patterns

# Python's \b does not work after Devanagari vowel signs (they are not \w), so word
# edges are written as explicit look-arounds over Latin and Devanagari letters.
_WORD = r"A-Za-z0-9À-ɏऀ-ॿ"
_START = rf"(?<![{_WORD}])"
_END = rf"(?![{_WORD}])"
_QUOTE = "\"'‘’“”"
_YES_WORD = r"(?:yes|y|haa?n|haa|haan\s*ji|j(?:i|ee)\s+haa?n|हा[ँं]|जी\s+हा[ँं])"


def _alts(words: Sequence[str]) -> str:
    """``words`` as one alternation bounded by word edges."""
    return _START + "(?:" + "|".join(words) + ")" + _END


# Devanagari combining marks written as escapes, so the patterns do not depend on how an
# editor composes them: nukta (ज़ = ज + nukta after NFKC), candrabindu, anusvara.
_NUKTA = "़"
_NASAL = "[ँं]"

# Cues that ask for confirmation by themselves (questions and explicit yes/no prompts).
_EXPLICIT_REQUEST_RE = re.compile(
    "|".join(
        [
            _alts(
                [
                    r"shall i",
                    r"should i",
                    r"(?:would you like|do you want|do you wish) me to",
                    r"(?:would you like|do you want|do you wish|do you still want) to\s+"
                    r"(?:cancel|return|exchange|modify|change|update|book|process)",
                    r"(?:can|may) i\s+(?:go ahead|proceed|process|cancel|return|exchange|modify|"
                    r"change|update|book)",
                    r"(?:does|do) (?:everything|this|that|it|these|all of this) look\s+"
                    r"(?:good|right|correct|ok(?:ay)?|fine)",
                    r"(?:is|are) (?:that|this|these|those|everything|all|the (?:above|details|"
                    r"information))[^.?!\n]{0,40}?(?:correct|right|accurate)",
                    r"yes\s*(?:/|or)\s*no",
                    r"y/n",
                    # romanized Hindi: "haan ya nahi", "kya main ... kar doon / karun",
                    # "kya aap chahte hain ki main ...", "kya yeh sahi hai"
                    r"haa?n\s+(?:ya|yaa)\s+nahi\w*",
                    r"kya\s+(?:main|mai|hum)\s.{0,40}?kar\s*(?:d(?:oo|o|u)n|(?:oo|u)n)",
                    r"kya\s+aap\s+chaa?ht[ei]\s+hai?n\s+ki\s+(?:main|mai|hum)",
                    r"kya\s+aap\s.{0,40}?karna\s+chaa?ht[ei]\s+hai?n",
                    r"kya\s+(?:main|mai|hum)\s.{0,40}?kar\s+sakt[aei]",
                    r"kya\s+(?:yeh|ye|yah)\s+(?:sab\s+)?(?:sahi|theek|thik)\s+hai",
                ]
            ),
            # quoted yes: reply with "yes" / कृपया "हाँ" कहें
            rf"[{_QUOTE}]\s*{_YES_WORD}\s*[.!]?\s*[{_QUOTE}]",
            # Devanagari: "haan ya nahin"; "kya main ... kar doon / karoon?";
            # "kya aap chahte hain ki main ..."; "kya yah sahi hai" / "yah sahi hai?"
            rf"हा{_NASAL}\W{{0,3}}\s*या\s*\W{{0,3}}नहीं",
            rf"क्या\s+(?:मैं|हम)\s.{{0,40}}?(?:(?:कर|करवा)\s+दू{_NASAL}?|करू{_NASAL})",
            r"क्या\s+आप\s+चाहत[ेी]\s+हैं\s+कि\s+(?:मैं|हम)",
            r"क्या\s+आप\s.{0,40}?(?:करना|करवाना)\s+चाहत[ेी]\s+हैं",
            r"क्या\s+(?:मैं|हम)\s.{0,40}?कर\s+सकत[ाीे]",
            r"क्या\s+(?:(?:यह|ये)\s+)?(?:सब\s+|सभी\s+)?(?:सही|ठीक)\s+है",
            r"(?:यह|ये)\s+(?:सब\s+|सभी\s+)?(?:सही|ठीक)\s+है\s*\?",
        ]
    ),
    re.IGNORECASE,
)
# Bare cues ("confirm", "proceed", "पुष्टि", ...) also occur in statements, list items and
# headers ("we will confirm the items", "### Options to proceed"); they count only in a
# sentence with request syntax (_REQUEST_SYNTAX_RE) that is not a list item or header.
_BARE_REQUEST_RE = re.compile(
    _alts(
        [
            r"confirm",  # base form only: "confirmed"/"confirming" are not requests
            r"(?:your|explicit) confirmation",
            r"proceed",
            r"go ahead",
            r"pushti",
            r"aage\s+badh\w*",
        ]
    )
    + rf"|पुष्टि|कन्फ{_NUKTA}?र्म|आगे\s+बढ{_NUKTA}?",
    re.IGNORECASE,
)
_REQUEST_SYNTAX_RE = re.compile(
    r"\?\W*$|"
    + _alts(
        [
            r"please",
            r"pls",
            r"kindly",
            r"let me know",
            r"reply",
            r"respond",
            r"(?:could|can|would|will|do) you",
            r"(?:if|once|after|when|as soon as) you",  # "I can cancel it if you confirm"
            r"before i",
            r"(?:need|require|await|awaiting|waiting for) your",
            r"kripya",
            r"krupya",
            r"karein",
            r"kijiye",
            r"kijie",
            r"bataiye",
            r"bataye",
            r"batayein",
            r"dijiye",
        ]
    )
    + r"|"
    + _alts(
        [
            r"कृपया",
            r"करें",
            r"कीजिए",
            r"कीजिये",
            r"करिए",
            r"करिये",
            rf"बताए{_NASAL}",
            r"बताइए",
            r"बताइये",
            r"दीजिए",
            r"दीजिये",
        ]
    ),
    re.IGNORECASE,
)
_LIST_OR_HEADER_RE = re.compile(r"^\s*(?:[-*•+]|\d+[.)]|#+)\s")
# Not requests, removed before matching: refusals ("I can't proceed without ..."),
# first-person announcements ("I'll proceed with the return ...", "I can confirm that
# ...", "we will confirm the items") and sentence-initial infinitives ("To confirm, you
# want ...", "To proceed with a return, the order must ..."). On the real RQ1 files the
# announcements were the only loose-flag hits on passing simulations.
_NOT_A_REQUEST_RE = re.compile(
    r"^\W*(?:just\s+)?to\s+(?:confirm|proceed|go ahead)"
    + _END
    + "|"
    + _START
    + r"(?:(?:can ?not|can'?t|cannot|unable to|not able to|won'?t be able to)\s+"
    + r"(?:\w+\s+){0,2}?(?:proceed|confirm|go ahead)"
    + r"|(?:i|we)\s*(?:'ll|will|shall|am going to|'m going to|are going to|'re going to)\s+"
    + r"(?:now\s+|then\s+)?(?:proceed|go ahead)"
    + r"|(?:i|we)\s*(?:can|could|will|'ll|would|'d|am able to)\s+(?:(?!you\b)[\w']+\s+){0,2}?"
    + r"confirm)"
    + _END,
    re.IGNORECASE,
)
# Sentences that are only sign-off offers ("anything else?") are not confirmation requests.
_CLOSING_RE = re.compile(
    r"anything else|any (?:other|more|further|additional) (?:questions?|help|assistance|"
    r"concerns?|requests?|changes?|modifications?|updates?)|further (?:assistance|help|"
    r"questions?)|feel free|have a (?:great|nice|good|wonderful) day|और कोई|कुछ और|aur koi|"
    r"kuch aur",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।])\s+|\n+")
_ALTERNATIVE_SPLIT_RE = re.compile(r",\s*(?:or|या)\s+", re.IGNORECASE)
# A confirmation request made after a write already ran is only about a pending action if
# a sentence carrying the cue names a write ("Shall I also cancel order #W1?"); offers
# such as "Would you like me to generate that link?" are not.
_WRITE_INTENT_RE = re.compile(
    _alts(
        [
            r"cancel\w*",
            r"return\w*",
            r"exchang\w*",
            r"modif\w*",
            r"chang\w*",
            r"updat\w*",
            r"book\w*",
            r"refund\w*",
            r"process\w*",
            r"replac\w*",
            r"switch\w*",
            r"swap\w*",
            r"upgrad\w*",
            r"downgrad\w*",
            r"wapas",
            r"vapas",
            r"badal\w*",
        ]
    )
    + rf"|रद्द|कैंस[लि]|वापस|वापसी|रिटर्न|एक्सचेंज|विनिमय|बदल|अपडेट|रिफ{_NUKTA}?ंड|बुक",
    re.IGNORECASE,
)

# The Hindi persona habitually opens with "जी," / "ji," (7 of 8 turns in the smoke run);
# it is skipped before the start-of-message checks, and a bare "ji" is not a yes.
_POLITE_PREFIX = rf"(?:(?:जी|ji|jee)(?![{_WORD}])\W*)?"
_NEGATIVE_START_RE = re.compile(
    r"^\W*"
    + _POLITE_PREFIX
    + r"(?:"
    + _alts(
        [
            r"no(?!\s+(?:problem|worries))",
            r"nope",
            r"nah",
            r"not(?!\s+a\s+problem)",
            r"don'?t",
            r"do not",
            r"wait",
            r"hold on",
            r"actually",
            r"nahi\w*",
            r"mat",
        ]
    )
    + r"|नहीं|नही|रुक)",
    re.IGNORECASE,
)
_AFFIRM_START_RE = re.compile(
    r"^\W*"
    + _POLITE_PREFIX
    + r"(?:"
    + _alts(
        [
            r"yes",
            r"yeah",
            r"yep",
            r"yup",
            r"sure",
            r"ok",
            r"okay",
            r"alright",
            r"all right",
            r"correct",
            r"confirmed",
            r"absolutely",
            r"definitely",
            r"of course",
            r"haa?n(?:\s*ji)?",
            r"haa",
            r"ha\s+ji",
            r"j(?:i|ee)\s*,?\s*haa?n?",
            r"bilkul",
            r"th(?:ee|i)k\s+hai",
            r"zaroor",
            r"jaroor",
        ]
    )
    + r"|हा[ँं]|जी\s*,?\s*हा[ँं]|ठीक\s+है|बिल्कुल|ज़रूर|जरूर)",
    re.IGNORECASE,
)
# Agreement phrases anywhere in the message. A match counts only when no negator
# precedes it in its clause and none follows it (_is_negated): "please do not cancel",
# "I'd rather not go ahead", "confirm nahi". A bare "confirm" is not agreement ("I can't
# confirm that right now"); "i confirm" and "confirm karta hoon" are.
_AFFIRM_ANYWHERE_RE = re.compile(
    _alts(
        [
            r"go ahead",
            r"please proceed",
            r"proceed with",
            r"i confirm",
            r"please do",
            r"do it",
            r"sounds good",
            r"that'?s (?:right|correct|fine|good)",
            r"that works",
            r"(?:yeh |ye |sab )?sahi hai",
            r"please (?:cancel|return|exchange|modify|change|update|book|process)",
            r"kar (?:dijiye|dijie|do|dein|den|dena)",
            r"confirm kar\w*",
            r"aage badh\w*",
        ]
    )
    + r"|कर दीजिए|कर दीजिये|कर दें|कर दो|कीजिए|पुष्टि कर|(?:(?:यह|ये|सब)\s+)?सही\s+है",
    re.IGNORECASE,
)
# Negators checked in the few words before an agreement phrase (same clause) and in the
# word right after it. After a verb, Hindi "na" / "न" is usually a tag ("kar dijiye na"),
# so it only counts before the phrase.
_NEGATOR_BEFORE_RE = re.compile(
    r"not|never|no|nope|cannot|n'?t|\w+n't|dont|cant|wont|didnt|nahi\w*|nahin|mat|na|"
    r"नहीं|नही|न|मत|ना",
    re.IGNORECASE,
)
_NEGATOR_AFTER_RE = re.compile(r"not|never|nahi\w*|nahin|mat|नहीं|नही|मत", re.IGNORECASE)
_NEGATION_WINDOW = 4
_CLAUSE_BREAK_RE = re.compile(r"[,;:.!?।\n—–]|\s-\s|\bbut\b|लेकिन", re.IGNORECASE)
_NOT_A_NEGATION_RE = re.compile(r"no (?:problem|worries)|not a problem", re.IGNORECASE)
_TOKEN_EDGE_CHARS = "\"'‘’“”()[]*_.,!?;:"
# Explicit refusals or withdrawals anywhere in the message veto any agreement:
# "Okay, actually no, don't cancel", "Mujhe cancel nahi karna", "रद्द न करें".
_ACTION = (
    r"(?:cancel|return|exchange|modify|change|update|book|process|proceed|go ahead|"
    r"confirm|do (?:it|that|this|anything))"
)
_REFUSAL_RE = re.compile(
    _START
    + r"(?:(?:do ?n'?t|do not|please don'?t|never|rather not)\s+"
    + _ACTION
    + r"|(?:can ?not|can'?t|cannot|won'?t|wont)\s+"
    + _ACTION
    + r"|not\s+(?:\w+\s+)?sure\s+(?:if|whether|that|about)"
    + r"|changed? my mind|never\s?mind|hold off|on second thought"
    + r"|(?:mat|nahi\w*|nahin|na)\s+(?:kar\w*|kijiye|kijie))"
    + _END
    + r"|(?:मत|नहीं|नही|न)\s+(?:कर[ऀ-ॿ]*|की[ऀ-ॿ]*)|रहने\s+(?:दीजिए|दीजिये|दें|दो)",
    re.IGNORECASE,
)
_TRANSFER_REQUEST_RE = re.compile(
    _alts(
        [
            r"(?:human|live|real)[\s-]+(?:agent|representative|rep|person|being|operator|support)",
            r"(?:speak|talk|chat)\w*\s+(?:to|with)\s+(?:a\s+|an\s+|your\s+|the\s+|some\s+)?"
            r"(?:human|person|representative|supervisor|manager|someone|somebody|real person)",
            r"transfer\s+me",
            r"(?:connect|escalate)\s+me",
            r"supervisor",
            r"insaa?n",
            r"kisi\s+(?:insaa?n|aadmi|aur)\s+se\s+baat",
            r"manager\s+se\s+baat",
        ]
    )
    + rf"|इंसान|मानव\s+(?:एजेंट|प्रतिनिधि)|सुपरवाइज{_NUKTA}?र|मैनेजर\s+से",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- text helpers


def message_text(msg: Mapping[str, Any]) -> str:
    """Text content of a message ('' for None; list content is joined)."""
    content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") if isinstance(p, Mapping) else str(p) for p in content]
        return " ".join(p for p in parts if p)
    return str(content)


def _normalize(text: str) -> str:
    """NFKC (narrow no-break spaces become spaces) and lower case."""
    return unicodedata.normalize("NFKC", text).lower()


def strip_special_tokens(text: str) -> str:
    """Remove ###STOP### / ###TRANSFER### / ###OUT-OF-SCOPE### and surrounding blanks."""
    for token in SPECIAL_TOKENS:
        text = text.replace(token, " ")
    return text.strip()


def _has_content(text: str) -> bool:
    """True when the text has at least one letter or digit (not just punctuation)."""
    return any(ch.isalnum() for ch in text)


def _sentence_cue(sentence: str) -> str | None:
    """The confirmation cue of one normalized sentence, or None.

    Explicit cues (questions such as "shall I", quoted "yes", "क्या मैं ... कर दूँ") count
    as they are; bare cues ("confirm", "proceed", "पुष्टि") only in a sentence with request
    syntax that is not a list item or header. The leftmost cue is returned.
    """
    if not sentence.strip():
        return None
    is_list_or_header = bool(_LIST_OR_HEADER_RE.match(sentence))
    # "Please confirm ..., or let me know if there's anything else": only the sign-off
    # alternative is skipped. Sentences without a sign-off are not split.
    parts = _ALTERNATIVE_SPLIT_RE.split(sentence) if _CLOSING_RE.search(sentence) else [sentence]
    for part in parts:
        if not part.strip() or _CLOSING_RE.search(part):
            continue
        cleaned = _NOT_A_REQUEST_RE.sub(" ", part)
        matches = [m for m in (_EXPLICIT_REQUEST_RE.search(cleaned),) if m]
        bare = _BARE_REQUEST_RE.search(cleaned)
        if bare and _REQUEST_SYNTAX_RE.search(part) and not is_list_or_header:
            matches.append(bare)
        if matches:
            return min(matches, key=lambda m: m.start()).group(0).strip()
    return None


def confirmation_sentences(agent_text: str) -> list[tuple[str, str]]:
    """``(cue, sentence)`` for every sentence of ``agent_text`` that asks for confirmation.

    Sentences that only offer more help ("anything else?", "feel free to ask") are
    skipped; refusals ("I can't proceed without ..."), announcements ("I'll proceed with
    the return", "I can confirm that ...") and sentence-initial "To confirm, ..." are
    removed first; bare cues need request syntax (:func:`_sentence_cue`).
    """
    out: list[tuple[str, str]] = []
    for sentence in _SENTENCE_SPLIT_RE.split(_normalize(agent_text)):
        cue = _sentence_cue(sentence)
        if cue is not None:
            out.append((cue, sentence.strip()))
    return out


def confirmation_cue(agent_text: str) -> str | None:
    """The phrase that makes ``agent_text`` a confirmation request, or None.

    The cue of the first sentence returned by :func:`confirmation_sentences`.
    """
    hits = confirmation_sentences(agent_text)
    return hits[0][0] if hits else None


def _edge_stripped(token: str) -> str:
    return token.strip(_TOKEN_EDGE_CHARS)


def _is_negated(text: str, match: re.Match[str]) -> bool:
    """True when a negator precedes ``match`` in its clause or directly follows it.

    Looks at up to :data:`_NEGATION_WINDOW` words before the phrase, stopping at a
    clause break (comma, full stop, "but", ...), after removing "no problem"; and at the
    first word after it ("please do not ...", "confirm nahi ...").
    """
    before = _NOT_A_NEGATION_RE.sub(" ", text[: match.start()])
    clause = _CLAUSE_BREAK_RE.split(before)[-1]
    window = [_edge_stripped(t) for t in clause.split()][-_NEGATION_WINDOW:]
    if any(_NEGATOR_BEFORE_RE.fullmatch(t) for t in window if t):
        return True
    after = text[match.end() :].split()
    return bool(after) and bool(_NEGATOR_AFTER_RE.fullmatch(_edge_stripped(after[0])))


def affirmation_cue(customer_text: str) -> str | None:
    """The phrase that makes ``customer_text`` agree to a pending request, or None.

    Special tokens are ignored. A message that opens with a refusal or hesitation
    ("no", "wait", "actually", "nahi", "नहीं"), possibly after the politeness marker
    "ji" / "जी", is never an affirmation, nor is one that refuses or withdraws anywhere
    ("don't cancel", "I'd rather not go ahead", "changed my mind", "cancel nahi karna",
    "रद्द न करें"). An agreement phrase in mid-message counts only when it is not negated
    (:func:`_is_negated`). A bare "ji" / "जी" is a politeness marker, not a yes.
    """
    text = _normalize(strip_special_tokens(customer_text))
    if not _has_content(text) or _NEGATIVE_START_RE.search(text) or _REFUSAL_RE.search(text):
        return None
    match = _AFFIRM_START_RE.search(text)
    if match is None:
        match = next(
            (m for m in _AFFIRM_ANYWHERE_RE.finditer(text) if not _is_negated(text, m)), None
        )
    return match.group(0).strip(" \t\n.,!") if match else None


def transfer_cue(customer_text: str) -> str | None:
    """The phrase in which the customer asks for a human / transfer, or None."""
    match = _TRANSFER_REQUEST_RE.search(_normalize(strip_special_tokens(customer_text)))
    return match.group(0).strip() if match else None


def _letter_script(ch: str) -> str | None:
    """'devanagari', 'latin', 'other' for letters and marks; None for anything else."""
    if not unicodedata.category(ch).startswith(("L", "M")):
        return None
    if "ऀ" <= ch <= "ॿ":
        return "devanagari"
    if unicodedata.name(ch, "").startswith("LATIN"):
        return "latin"
    return "other"


def script_counts(text: str) -> dict[str, int]:
    """Count Devanagari / Latin / other letters, skipping id-like tokens.

    Special tokens are replaced by a space first, so a word stuck to one
    (``"धन्यवाद###STOP###"``) still counts. Whitespace-separated tokens that then contain
    a digit, ``@``, ``#`` or ``_`` are skipped entirely (order and item ids, emails, user
    ids, prices).
    """
    counts = {"devanagari": 0, "latin": 0, "other": 0}
    for token in strip_special_tokens(text).split():
        if any(ch.isdigit() or ch in "@#_" for ch in token):
            continue
        for ch in token:
            script = _letter_script(ch)
            if script is not None:
                counts[script] += 1
    return counts


# --------------------------------------------------------------------------- stop checks


@dataclass(frozen=True)
class StopCheck:
    """Result of :func:`check_premature_stop` for one simulation."""

    premature_stop: bool = False
    stop_after_unexecuted_confirmation: bool = False
    confirmation_cue: str | None = None
    affirmation_cue: str | None = None
    stop_message: str | None = None
    stop_index: int | None = None
    confirmation_index: int | None = None


def _is_write(name: str | None, write_tools: frozenset[str]) -> bool:
    return bool(name) and name in write_tools


def _write_calls_from(
    messages: Sequence[Mapping[str, Any]],
    start: int,
    write_tools: frozenset[str],
    end: int | None = None,
) -> list[str]:
    """Names of agent write tool calls in ``messages[start:end]``."""
    names = []
    for msg in messages[start:end]:
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            if _is_write(call.get("name"), write_tools):
                names.append(call["name"])
    return names


def _preceding_agent_text(messages: Sequence[Mapping[str, Any]], idx: int) -> int | None:
    """Index of the last assistant message with text before ``idx`` in the same exchange.

    Tool results, tool-call-only assistant messages and text-less customer messages
    (user-side tool calls, e.g. in telecom) are skipped; reaching another customer
    message with text first means no agent text preceded this one (returns None).
    """
    for j in range(idx - 1, -1, -1):
        msg = messages[j]
        role = msg.get("role")
        has_text = bool(message_text(msg).strip())
        if role == "user" and has_text:
            return None
        if role == "assistant" and has_text:
            return j
    return None


def _pending_write_request(
    messages: Sequence[Mapping[str, Any]],
    j: int,
    hits: Sequence[tuple[str, str]],
    write_tools: frozenset[str],
) -> bool:
    """Whether the confirmation request at ``messages[j]`` is about a write not yet run.

    Before any write, a confirmation request is taken to be about the task's write. Once
    a write has run, the request must name a write in a sentence that carries the cue
    ("Shall I also cancel order #W1?"); otherwise it is an offer or a recap after the
    work was done ("Would you like me to generate that link?") and nothing is pending.
    """
    if not _write_calls_from(messages, 0, write_tools, end=j):
        return True
    return any(_WRITE_INTENT_RE.search(sentence) for _, sentence in hits)


def _trim(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def check_premature_stop(
    messages: Sequence[Mapping[str, Any]], write_tools: Iterable[str] | None = None
) -> StopCheck:
    """Apply the strict and loose STOP-after-confirmation checks (module docstring).

    Args:
        messages: The simulation's ``messages`` list.
        write_tools: Tool names that change the DB. Default :data:`DEFAULT_WRITE_TOOLS`.

    Returns:
        A :class:`StopCheck`. When several customer messages carry ``###STOP###``, the
        first one that qualifies is reported (strict before loose); otherwise the
        last STOP message is reported with both flags False.
    """
    tools = frozenset(write_tools) if write_tools is not None else DEFAULT_WRITE_TOOLS
    stop_idxs = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "user" and STOP_TOKEN in message_text(m)
    ]
    best: StopCheck | None = None
    for idx in stop_idxs:
        text = message_text(messages[idx])
        j = _preceding_agent_text(messages, idx)
        hits = confirmation_sentences(message_text(messages[j])) if j is not None else []
        cue = hits[0][0] if hits else None
        loose = (
            j is not None
            and cue is not None
            and _pending_write_request(messages, j, hits, tools)
            and not _write_calls_from(messages, j, tools)
        )
        affirm = affirmation_cue(text) if loose else None
        check = StopCheck(
            premature_stop=loose and affirm is not None,
            stop_after_unexecuted_confirmation=loose,
            confirmation_cue=cue,
            affirmation_cue=affirm,
            stop_message=_trim(text),
            stop_index=idx,
            confirmation_index=j if cue is not None else None,
        )
        if check.premature_stop:
            return check
        if loose and (best is None or not best.stop_after_unexecuted_confirmation):
            best = check
    if best is not None:
        return best
    if stop_idxs:
        return StopCheck(stop_message=_trim(message_text(messages[stop_idxs[-1]])))
    return StopCheck()


# --------------------------------------------------------------------------- per simulation


def infer_user_language(
    info: Mapping[str, Any],
    task_id: str | None = None,
    sim: Mapping[str, Any] | None = None,
) -> str | None:
    """Language code of the simulated user, or None for a plain (English) tau2 run.

    Looks at the simulation's ``speech_environment.language`` (recorded per simulation
    by the tau-multilingual branch; None on tau2 main), then ``info.user_persona_id``
    (bare code like ``"hi"`` or a pack persona like ``"rishika_hindi_v1"``), then a
    ``_<code>`` suffix of ``info.task_set_name`` (``retail_hi``), then of the task id
    (``0_hi``).
    """
    sim_language = str(((sim or {}).get("speech_environment") or {}).get("language") or "")
    if re.fullmatch(r"[a-z]{2,3}", sim_language.lower()):
        return sim_language.lower()
    persona = str(info.get("user_persona_id") or "").lower()
    if re.fullmatch(r"[a-z]{2,3}", persona):
        return persona
    for name, code in _LANGUAGE_NAMES.items():
        if name in persona:
            return code
    for value in (info.get("task_set_name"), task_id):
        match = re.search(r"_([a-z]{2,3})$", str(value or "").lower())
        if match:
            return match.group(1)
    return None


def leak_script_for(language: str | None, style: str | None) -> str | None:
    """Which script counts as leakage for this language and text input style.

    ``style`` None means ``native_script`` on the tau-multilingual branch, but only when a
    language pack is in use (``language`` not None).
    """
    if language not in DEVANAGARI_LANGUAGES:
        return None
    effective = style or "native_script"
    if effective in ROMAN_STYLES:
        return "devanagari"
    if effective == "native_script":
        return "latin"
    return None


def _write_tools_for(sim: Mapping[str, Any], extra: Iterable[str] | None) -> frozenset[str]:
    gold = {
        c["action"]["name"]
        for c in ((sim.get("reward_info") or {}).get("action_checks") or [])
        if c.get("tool_type") == "write" and (c.get("action") or {}).get("name")
    }
    return DEFAULT_WRITE_TOOLS | gold | frozenset(extra or ())


def simulation_fidelity(
    sim: Mapping[str, Any],
    info: Mapping[str, Any] | None = None,
    write_tools: Iterable[str] | None = None,
    native_latin_share_max: float = NATIVE_LATIN_SHARE_MAX,
) -> dict[str, Any]:
    """All fidelity columns (:data:`FIDELITY_COLUMNS`) for one tau2 simulation dict.

    Args:
        sim: One entry of ``results.json["simulations"]``.
        info: The run's ``results.json["info"]`` (for ``text_input_style`` and language).
        write_tools: Extra write tool names, added to :data:`DEFAULT_WRITE_TOOLS` and to
            the gold write actions of this simulation.
        native_latin_share_max: Latin-letter share above which a native-script run counts
            as leaking.
    """
    info = info or {}
    messages: list[Mapping[str, Any]] = list(sim.get("messages") or [])
    customer = [message_text(m) for m in messages if m.get("role") == "user"]
    customer = [t for t in customer if t.strip()]
    agent_turns = sum(
        1 for m in messages if m.get("role") == "assistant" and message_text(m).strip()
    )

    end_token = None
    stop_with_content = False
    for text in reversed(customer):
        found = [label for token, label in SPECIAL_TOKENS.items() if token in text]
        if found:
            end_token = found[0]
            stop_with_content = STOP_TOKEN in text and _has_content(strip_special_tokens(text))
            break

    stop = check_premature_stop(messages, _write_tools_for(sim, write_tools))

    transfer_cues = [c for c in (transfer_cue(t) for t in customer) if c]
    agent_transferred = any(
        call.get("name") == TRANSFER_TOOL
        for m in messages
        if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])
    )

    per_turn = [script_counts(t) for t in customer]
    dev = sum(c["devanagari"] for c in per_turn)
    lat = sum(c["latin"] for c in per_turn)
    letters = sum(sum(c.values()) for c in per_turn)
    style = info.get("text_input_style")
    language = infer_user_language(info, str(sim.get("task_id") or ""), sim)
    leak = leak_script_for(language, style)
    if leak is None:
        leak_share: float = float("nan")
        leak_turns: Any = pd.NA
        leakage: Any = pd.NA
    else:
        leak_letters = dev if leak == "devanagari" else lat
        leak_share = leak_letters / letters if letters else float("nan")
        leak_turns = sum(1 for c in per_turn if c[leak] > 0)
        if leak == "devanagari":
            leakage = leak_turns > 0
        else:
            leakage = bool(letters) and leak_share > native_latin_share_max

    return {
        "customer_turns": len(customer),
        "agent_turns": agent_turns,
        "end_token": end_token,
        "stop_with_content": stop_with_content,
        "premature_stop": stop.premature_stop,
        "stop_after_unexecuted_confirmation": stop.stop_after_unexecuted_confirmation,
        "confirmation_cue": stop.confirmation_cue,
        "affirmation_cue": stop.affirmation_cue,
        "stop_message": stop.stop_message,
        "transfer_requested_by_customer": bool(transfer_cues),
        "customer_transfer_turns": len(transfer_cues),
        "transfer_cue": transfer_cues[0] if transfer_cues else None,
        "agent_transferred": agent_transferred,
        "text_input_style": style,
        "user_language": language,
        "leak_script": leak,
        "customer_letters": letters,
        "devanagari_letters": dev,
        "latin_letters": lat,
        "devanagari_share": dev / letters if letters else float("nan"),
        "latin_share": lat / letters if letters else float("nan"),
        "customer_turns_with_devanagari": sum(1 for c in per_turn if c["devanagari"] > 0),
        "customer_turns_with_latin": sum(1 for c in per_turn if c["latin"] > 0),
        "script_leak_share": leak_share,
        "script_leak_turns": leak_turns,
        "script_leakage": leakage,
    }


# --------------------------------------------------------------------------- whole frames


def fidelity_frame(
    paths: str | Path | Iterable[str | Path],
    write_tools: Iterable[str] | None = None,
    native_latin_share_max: float = NATIVE_LATIN_SHARE_MAX,
) -> pd.DataFrame:
    """One row per simulation (file order) with :data:`KEY_COLUMNS` + fidelity columns.

    ``results_path`` and ``run_name`` are derived exactly as in
    :func:`whose_noise.tau2_results.load_runs`, so the frames join on them.
    """
    if isinstance(paths, str | Path):
        paths = [paths]
    rows: list[dict[str, Any]] = []
    for path in paths:
        results_path = resolve_results_path(path)
        data = read_results(results_path)
        info = data.get("info") or {}
        for sim in data.get("simulations") or []:
            row = {
                "results_path": str(results_path),
                "run_name": results_path.parent.name,
                "task_id": str(sim.get("task_id")),
                "trial": int(sim.get("trial") or 0),
            }
            row.update(simulation_fidelity(sim, info, write_tools, native_latin_share_max))
            rows.append(row)
    out = pd.DataFrame(rows, columns=[*KEY_COLUMNS, *FIDELITY_COLUMNS])
    out["script_leakage"] = out["script_leakage"].astype("boolean")
    out["script_leak_turns"] = out["script_leak_turns"].astype("Int64")
    for col in ("devanagari_share", "latin_share", "script_leak_share"):
        out[col] = out[col].astype(float)
    return out


def _with_occurrence(df: pd.DataFrame) -> pd.DataFrame:
    """Number repeated (results_path, task_id, trial) rows 0, 1, ... in frame order."""
    keys = ["results_path", "task_id", "trial"]
    return df.assign(**{_OCC: df.groupby(keys, sort=False).cumcount()})


def add_fidelity(
    df: pd.DataFrame,
    write_tools: Iterable[str] | None = None,
    native_latin_share_max: float = NATIVE_LATIN_SHARE_MAX,
) -> pd.DataFrame:
    """Left-join the fidelity columns onto a frame from ``load_runs``.

    The results files named in ``df["results_path"]`` are read again (the loaded frame
    keeps no messages). Rows join on ``run_name``, ``task_id``, ``trial`` (and the file
    path); duplicated trials loaded with ``on_duplicates='ignore'`` are matched in file
    order. Existing fidelity columns in ``df`` are replaced. Rows missing from the file
    (e.g. it changed since loading) get missing values.
    """
    base = df.drop(columns=[c for c in FIDELITY_COLUMNS if c in df.columns])
    if base.empty:
        return base.assign(**{c: pd.Series(dtype=object) for c in FIDELITY_COLUMNS})
    fid = fidelity_frame(
        list(dict.fromkeys(base["results_path"])), write_tools, native_latin_share_max
    )
    merged = _with_occurrence(base).merge(
        _with_occurrence(fid), on=[*KEY_COLUMNS, _OCC], how="left", validate="one_to_one"
    )
    return merged.drop(columns=[_OCC])


def _is_pass(success: pd.Series) -> pd.Series:
    """Element-wise ``success is True`` for a nullable boolean column (NA -> False)."""
    return success.astype("boolean").fillna(False).astype(bool)


def fidelity_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Per run and outcome (``pass`` / ``fail`` / ``all``): counts and rates of each flag.

    ``df`` must come from :func:`add_fidelity`. Infrastructure errors are excluded
    (their outcome is unknown and their transcripts are often cut short). Rows whose
    transcript was not found when the file was re-read (``customer_turns`` missing) are
    left out of every rate and counted in ``unmatched``, so a missing transcript is never
    counted as "not flagged". The ``script_leakage`` rate is over the simulations where it
    applies (``script_leak_applicable``); it is NaN when none do.
    """
    rows: list[dict[str, Any]] = []
    for _, run in df.groupby("results_path", sort=False):
        not_infra = run[~run["is_infra_error"].astype(bool)]
        matched = not_infra["customer_turns"].notna()
        valid = not_infra[matched]
        ok = _is_pass(valid["success"])
        for label, sub, unmatched in (
            ("pass", valid[ok], int((~matched & _is_pass(not_infra["success"])).sum())),
            ("fail", valid[~ok], int((~matched & ~_is_pass(not_infra["success"])).sum())),
            ("all", valid, int((~matched).sum())),
        ):
            n = len(sub)
            row: dict[str, Any] = {
                "run_name": str(run["run_name"].iloc[0]),
                "results_path": str(run["results_path"].iloc[0]),
                "outcome": label,
                "sims": n,
                "unmatched": unmatched,
            }
            for flag in FLAG_COLUMNS:
                count = int(sub[flag].fillna(False).astype(bool).sum())
                row[flag] = count
                row[f"{flag}_rate"] = count / n if n else float("nan")
            applicable = sub["script_leakage"].notna()
            n_app = int(applicable.sum())
            leaks = int(sub.loc[applicable, "script_leakage"].astype(bool).sum())
            row["script_leak_applicable"] = n_app
            row["script_leakage"] = leaks
            row["script_leakage_rate"] = leaks / n_app if n_app else float("nan")
            row["mean_script_leak_share"] = (
                float(sub["script_leak_share"].mean()) if n_app else float("nan")
            )
            row["mean_customer_turns"] = float(sub["customer_turns"].mean()) if n else float("nan")
            row["mean_agent_turns"] = float(sub["agent_turns"].mean()) if n else float("nan")
            rows.append(row)
    return pd.DataFrame(rows)


def _count_rate(count: int, rate: float) -> str:
    return "-" if rate != rate else f"{count} ({rate:.2f})"  # NaN check without numpy


def _mean_txt(x: float, digits: int = 1) -> str:
    return "n/a" if x != x else f"{x:.{digits}f}"


def _flagged_ids(run_df: pd.DataFrame, flag: str) -> list[str]:
    sub = run_df[run_df[flag].fillna(False).astype(bool) & ~run_df["is_infra_error"]]
    outcome = ["pass" if ok else "fail" for ok in _is_pass(sub["success"])]
    return [
        f"task {t} trial {r} ({o})"
        for t, r, o in zip(sub["task_id"], sub["trial"], outcome, strict=True)
    ]


def format_fidelity(run_df: pd.DataFrame) -> str:
    """ASCII table of :func:`fidelity_summary` for one run plus the flagged simulations.

    ``run_df`` holds the rows of one run from :func:`add_fidelity`. Message text is not
    printed (it may hold non-ASCII script); use the CSV for the cues.
    """
    summary = fidelity_summary(run_df)
    if summary.empty:
        return "(no simulations)"
    table = pd.DataFrame(
        {
            "outcome": summary["outcome"],
            "sims": summary["sims"],
            "premature_stop": [
                _count_rate(c, r)
                for c, r in zip(
                    summary["premature_stop"], summary["premature_stop_rate"], strict=True
                )
            ],
            "stop_after_unexec_confirm": [
                _count_rate(c, r)
                for c, r in zip(
                    summary["stop_after_unexecuted_confirmation"],
                    summary["stop_after_unexecuted_confirmation_rate"],
                    strict=True,
                )
            ],
            "customer_asks_transfer": [
                _count_rate(c, r)
                for c, r in zip(
                    summary["transfer_requested_by_customer"],
                    summary["transfer_requested_by_customer_rate"],
                    strict=True,
                )
            ],
            "script_leakage": [
                _count_rate(c, r)
                for c, r in zip(
                    summary["script_leakage"], summary["script_leakage_rate"], strict=True
                )
            ],
            "leak_share": [_mean_txt(x, 3) for x in summary["mean_script_leak_share"]],
            "customer_turns": [_mean_txt(x) for x in summary["mean_customer_turns"]],
            "agent_turns": [_mean_txt(x) for x in summary["mean_agent_turns"]],
        }
    )
    styles = sorted({str(s) for s in run_df["text_input_style"].dropna()})
    leak_scripts = sorted({str(s) for s in run_df["leak_script"].dropna()})
    lines = [
        "counts (rate) per outcome; infra errors excluded; deterministic transcript checks "
        "(whose_noise.fidelity)",
        "text_input_style: "
        + (", ".join(styles) if styles else "none recorded")
        + "; leak script: "
        + (", ".join(leak_scripts) if leak_scripts else "n/a (no language-pack style)"),
        table.to_string(index=False),
    ]
    unmatched = int(summary.loc[summary["outcome"] == "all", "unmatched"].iloc[0])
    if unmatched:
        lines.append(
            f"WARNING: {unmatched} valid simulation(s) had no transcript when the results "
            "file was re-read (did it change since loading?); they are left out of the rates"
        )
    for flag in FLAG_COLUMNS:
        ids = _flagged_ids(run_df, flag)
        if ids:
            lines.append(f"{flag}: " + ", ".join(ids))
    return "\n".join(lines)
