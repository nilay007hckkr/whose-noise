"""Tests for whose_noise.fidelity (deterministic simulator-fidelity checks).

Two kinds of tests:

* synthetic message lists with a known answer for every flag (positive, negative and
  edge cases, including romanized and Devanagari Hindi);
* trimmed real transcripts in ``tests/fixtures/fidelity`` (regenerate them with
  ``tests/fixtures/make_fidelity_fixtures.py``). The gpt-oss-120b fixture holds the three
  "Yes.###STOP###" simulator errors found by the RQ1 chunk 1 failure audit.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from whose_noise import analyze, fidelity
from whose_noise.fidelity import (
    FIDELITY_COLUMNS,
    add_fidelity,
    affirmation_cue,
    check_premature_stop,
    confirmation_cue,
    fidelity_frame,
    fidelity_summary,
    format_fidelity,
    infer_user_language,
    leak_script_for,
    script_counts,
    simulation_fidelity,
    transfer_cue,
)
from whose_noise.tau2_results import load_runs

FID_DIR = Path(__file__).parent / "fixtures" / "fidelity"
GPTOSS = FID_DIR / "fid_rq1_gptoss120b"
QWEN38 = FID_DIR / "fid_rq1_qwen38"
HINDI = FID_DIR / "fid_hi_smoke"
GPTOSS_POST_WRITE = FID_DIR / "fid_rq1_gptoss120b_post_write"  # task 55 trial 0
PILOT_LINK = FID_DIR / "fid_pilot1_gptoss120b"  # task 1 trial 2

ASK = 'The exchange is ready. Please confirm with a "yes" if you\'d like me to proceed.'
SIGN_OFF = "Your exchange is done. Is there anything else I can help with? Feel free to ask!"

# ----------------------------------------------------------------- message builders


def agent(text: str | None = None, *calls: str) -> dict[str, Any]:
    tool_calls = [{"id": f"c{i}", "name": n, "arguments": {}} for i, n in enumerate(calls)]
    return {"role": "assistant", "content": text, "tool_calls": tool_calls or None}


def user(text: str | None, *calls: str) -> dict[str, Any]:
    tool_calls = [{"id": f"u{i}", "name": n, "arguments": {}} for i, n in enumerate(calls)]
    return {"role": "user", "content": text, "tool_calls": tool_calls or None}


def tool(text: str = "{}") -> dict[str, Any]:
    return {"role": "tool", "content": text, "tool_calls": None}


def convo(*tail: dict[str, Any]) -> list[dict[str, Any]]:
    """Greeting + an opening request + a read lookup, then ``tail``."""
    return [
        agent("Hi! How can I help you today?"),
        user("I want to exchange an item from order #W2378156."),
        agent(None, "get_order_details"),
        tool('{"order_id": "#W2378156", "status": "delivered"}'),
        *tail,
    ]


def sim(
    messages: list[dict[str, Any]],
    task_id: str = "1",
    trial: int = 0,
    reward: float | None = 1.0,
) -> dict[str, Any]:
    return {
        "id": f"{task_id}-{trial}",
        "task_id": task_id,
        "trial": trial,
        "seed": 7,
        "duration": 1.0,
        "termination_reason": "user_stop" if reward is not None else "infrastructure_error",
        "reward_info": None
        if reward is None
        else {"reward": reward, "db_check": {"db_match": reward == 1.0}, "action_checks": []},
        "messages": messages,
    }


def write_run(path: Path, sims: list[dict[str, Any]], info: dict[str, Any] | None = None) -> Path:
    data = {
        "timestamp": "t",
        "info": {
            "user_info": {"llm": "user-llm"},
            "agent_info": {"llm": "agent-llm"},
            "environment_info": {"domain_name": "retail"},
            **(info or {}),
        },
        "tasks": [],
        "simulations": sims,
    }
    path.mkdir(parents=True, exist_ok=True)
    (path / "results.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# ----------------------------------------------------------------- confirmation / affirmation


@pytest.mark.parametrize(
    ("text", "cue"),
    [
        (ASK, "confirm"),
        ("Would you like me to proceed?", "would you like me to"),
        ("Shall I go ahead?", "shall i"),
        ("Do you want me to cancel the order?", "do you want me to"),
        ("Is that correct?", "is that correct"),
        ("Kya main aage badhun?", "aage badhun"),  # romanized Hindi
        ("Please reply haan ya nahi.", "haan ya nahi"),
        ("क्या आप पुष्टि करते हैं?", "पुष्टि"),  # Devanagari "confirmation"
        ('कृपया "हाँ" कहें।', '"हाँ"'),  # quoted yes, as in the real Hindi smoke run
    ],
)
def test_confirmation_cue_positive(text: str, cue: str) -> None:
    assert confirmation_cue(text) == cue


@pytest.mark.parametrize(
    "text",
    [
        "Your exchange is confirmed.",  # past participle: a report, not a request
        "I can't proceed without verifying your identity. Could you give me your name?",
        "I'll proceed with the return now.",  # announcement
        SIGN_OFF,  # sign-off offer
        "Your order has been cancelled. Would you like help with anything else?",
        "",
    ],
)
def test_confirmation_cue_negative(text: str) -> None:
    assert confirmation_cue(text) is None


@pytest.mark.parametrize(
    ("text", "cue"),
    [
        ("Yes.###STOP###", "yes"),
        ("yes\n###STOP###", "yes"),
        ("Yes, please go ahead with the return.###STOP###", "yes"),
        ("Sure thing ###STOP###", "sure"),
        ("haan ###STOP###", "haan"),
        ("ji haan.###STOP###", "ji haan"),
        ("Haan ji, kar dijiye ###STOP###", "haan ji"),
        ("हाँ ###STOP###", "हाँ"),
        ("हां ###STOP###", "हां"),  # anusvara spelling
        ("जी हाँ, कर दीजिए ###STOP###", "जी हाँ"),
        ("No problem, go ahead.###STOP###", "go ahead"),  # "no problem" is not a refusal
    ],
)
def test_affirmation_cue_positive(text: str, cue: str) -> None:
    assert affirmation_cue(text) == cue


@pytest.mark.parametrize(
    "text",
    [
        "###STOP###",  # STOP alone
        "Thanks!###STOP###",
        "No, don't do that.###STOP###",
        "nahi ###STOP###",
        "नहीं, रहने दीजिए ###STOP###",
        "Actually wait ###STOP###",
        "ji ###STOP###",  # bare politeness marker
        "जी ###STOP###",
    ],
)
def test_affirmation_cue_negative(text: str) -> None:
    assert affirmation_cue(text) is None


# English request forms and their Devanagari / romanized-Hindi mirrors: recall has to be
# matched across languages before English and Hindi premature-stop rates are compared.
@pytest.mark.parametrize(
    ("english", "devanagari", "romanized"),
    [
        (  # "shall I <write>?"
            "Shall I cancel your order?",
            "क्या मैं आपका ऑर्डर रद्द कर दूँ?",
            "Kya main aapka order cancel kar doon?",
        ),
        (  # "do you want me to <write>?"
            "Do you want me to process this exchange?",
            "क्या आप चाहते हैं कि मैं यह एक्सचेंज कर दूँ?",
            "Kya aap chahte hain ki main yeh exchange kar doon?",
        ),
        (  # "do you want to <write>?"
            "Do you want to cancel the order?",
            "क्या आप ऑर्डर रद्द करना चाहते हैं?",
            "Kya aap order cancel karna chahte hain?",
        ),
        ("Shall I proceed?", "क्या मैं आगे बढ़ूँ?", "Kya main aage badhun?"),
        ("Is that correct?", "क्या यह सही है?", "Kya yeh sahi hai?"),
        (  # "can I <write>?"
            "Can I process this exchange for you now?",
            "क्या मैं यह एक्सचेंज कर सकता हूँ?",
            "Kya main yeh exchange kar sakta hoon?",
        ),
        ("Please confirm the exchange.", "कृपया एक्सचेंज की पुष्टि करें।", "Kripya pushti karein."),
    ],
)
def test_hindi_confirmation_requests_mirror_english(
    english: str, devanagari: str, romanized: str
) -> None:
    for text in (english, devanagari, romanized):
        assert confirmation_cue(text) is not None, text
        check = check_premature_stop(convo(agent(text), user("हाँ ###STOP###")))
        assert check.premature_stop, text


@pytest.mark.parametrize(
    "text",
    [
        "बढ़ूँ",  # not a question on its own
        "आपके ऑर्डर की स्थिति के लिए आगे बढ़ें।",  # real smoke agent turn: instruction, not request
        "हम सामान मिलने के बाद पुष्टि करेंगे।",  # "we will confirm after receiving the items"
        "मैंने आपके ऑर्डर की पुष्टि कर दी है।",  # "I have confirmed your order"
    ],
)
def test_hindi_statements_are_not_confirmation_requests(text: str) -> None:
    assert confirmation_cue(text) is None


@pytest.mark.parametrize(
    "text",
    [
        # real agent sentences from the RQ1 and pilot files: statements, list items, headers
        "However, I can confirm that:",
        "### Options to proceed:",
        "I will now check your payment methods once more to confirm the correct ID.",
        'To proceed with the return, the order must be in a "delivered" status.',
        "The refund will be processed after we receive and confirm the returned items.",
        "To proceed with your request, I will now:",
        "Your return is set. You'll be able to:\n- Track the parcel\n- Confirm it's received by us",
        "Just to confirm, your exchange has been processed.",
        "To confirm, you'd like to return the kettle.",
    ],
)
def test_bare_confirm_and_proceed_in_statements_are_not_requests(text: str) -> None:
    assert confirmation_cue(text) is None


@pytest.mark.parametrize(
    ("text", "cue"),
    [
        ("To confirm, you'd like to return the kettle. Please reply yes to proceed.", "proceed"),
        ("I can cancel the entire order if you confirm the reason.", "confirm"),
        ("Let me know if I should go ahead.", "go ahead"),
        ("Before I proceed, please confirm again with a yes.", "proceed"),
        # a request joined to a sign-off: only the sign-off alternative is skipped
        (
            "Please confirm if you'd like me to proceed with this exchange, or let me know "
            "if there's anything else I can help with.",
            "confirm",
        ),
        ("Does everything look good?", "does everything look good"),
    ],
)
def test_requests_with_request_syntax(text: str, cue: str) -> None:
    assert confirmation_cue(text) == cue


@pytest.mark.parametrize(
    "answer",
    [
        "Please do not cancel it. ###STOP###",
        "Please don't proceed with the cancellation. ###STOP###",
        "I can't confirm that right now; I need to check with my wife first. ###STOP###",
        "I don't want to confirm the cancellation. ###STOP###",
        "I've changed my mind, please do not go ahead. ###STOP###",
        "Thanks, but I'd rather not go ahead with it. ###STOP###",
        "Okay, actually no, don't cancel. ###STOP###",
        "I'm not entirely sure if that's correct. ###STOP###",
        "Main abhi confirm nahi kar sakta. ###STOP###",
        "Mujhe cancel nahi karna, please do not proceed. ###STOP###",
        "Ji nahi, rehne dijiye. ###STOP###",
        "जी, नहीं। ###STOP###",
        "कृपया रद्द न करें, मैं पुष्टि नहीं करता। ###STOP###",
        "रद्द मत कीजिए ###STOP###",
    ],
)
def test_refusals_with_agreement_words_are_loose_only(answer: str) -> None:
    ask = 'Please confirm with "yes" that I should cancel order #W1.'
    assert affirmation_cue(answer) is None, answer
    check = check_premature_stop(convo(agent(ask), user(answer)))
    assert not check.premature_stop and check.stop_after_unexecuted_confirmation, answer


@pytest.mark.parametrize(
    ("text", "cue"),
    [
        # the Hindi persona opens almost every turn with "जी," / "ji,"
        ("जी, ठीक है।###STOP###", "जी, ठीक है"),
        ("जी, बिल्कुल।###STOP###", "जी, बिल्कुल"),
        ("जी, ज़रूर ###STOP###", "जी, ज़रूर"),
        ("ji, theek hai ###STOP###", "ji, theek hai"),
        ("Ji bilkul ###STOP###", "ji bilkul"),
        ("Ha, sahi hai ###STOP###", "sahi hai"),
        ("Haan, kar dijiye na ###STOP###", "haan"),  # "na" after a verb is a tag, not "no"
        ("Not a problem, go ahead.###STOP###", "go ahead"),
        ("Yes, please proceed. I don't need anything else.###STOP###", "yes"),
    ],
)
def test_affirmations_after_politeness_prefix_and_with_tags(text: str, cue: str) -> None:
    assert affirmation_cue(text) == cue


# ----------------------------------------------------------------- premature STOP


def test_yes_stop_after_confirmation_request_is_premature() -> None:
    check = check_premature_stop(convo(agent(ASK), user("Yes.###STOP###")))
    assert check.premature_stop and check.stop_after_unexecuted_confirmation
    assert (check.confirmation_cue, check.affirmation_cue) == ("confirm", "yes")
    assert check.stop_message == "Yes.###STOP###"
    assert (check.confirmation_index, check.stop_index) == (4, 5)


@pytest.mark.parametrize(
    "answer", ["haan ###STOP###", "ji haan.###STOP###", "हाँ ###STOP###", "जी हाँ ###STOP###"]
)
def test_hindi_yes_stop_is_premature(answer: str) -> None:
    for ask in (ASK, "क्या आप इस बदलाव की पुष्टि करते हैं?", "Kya main aage badhun?"):
        check = check_premature_stop(convo(agent(ask), user(answer)))
        assert check.premature_stop, (ask, answer)


def test_stop_alone_after_confirmation_is_loose_only() -> None:
    check = check_premature_stop(convo(agent(ASK), user("###STOP###")))
    assert not check.premature_stop
    assert check.stop_after_unexecuted_confirmation
    assert check.affirmation_cue is None


@pytest.mark.parametrize("answer", ["No, don't.###STOP###", "nahi ###STOP###", "नहीं ###STOP###"])
def test_refusal_stop_after_confirmation_is_loose_only(answer: str) -> None:
    check = check_premature_stop(convo(agent(ASK), user(answer)))
    assert not check.premature_stop
    assert check.stop_after_unexecuted_confirmation


def test_stop_after_executed_write_is_not_flagged() -> None:
    # The usual good ending: yes -> write -> report -> thanks + STOP.
    messages = convo(
        agent(ASK),
        user("Yes, please."),
        agent(None, "exchange_delivered_order_items"),
        tool(),
        agent(SIGN_OFF),
        user("Yes, that's all, thanks.###STOP###"),
    )
    check = check_premature_stop(messages)
    assert not check.premature_stop and not check.stop_after_unexecuted_confirmation
    assert check.stop_message == "Yes, that's all, thanks.###STOP###"


def test_write_after_the_confirmation_text_clears_both_flags() -> None:
    # Write call on the confirmation message itself, or in a tool-call-only message
    # between it and the STOP: the action was executed, so nothing is premature.
    same_message = convo(
        agent(ASK, "exchange_delivered_order_items"), tool(), user("Yes###STOP###")
    )
    later_message = convo(
        agent(ASK), agent(None, "exchange_delivered_order_items"), tool(), user("Yes###STOP###")
    )
    for messages in (same_message, later_message):
        check = check_premature_stop(messages)
        assert not check.premature_stop and not check.stop_after_unexecuted_confirmation


def test_earlier_write_does_not_clear_a_request_for_another_write() -> None:
    # As in gpt-oss-120b task 32 trial 0: returns ran, then the agent asks to cancel.
    for ask in (
        "Cancelled. Shall I also exchange the lamp?",
        "Cancelled. Do you want to cancel the second order too?",
        "Cancelled. I can process the exchange as well if you confirm.",
    ):
        messages = convo(
            agent(None, "cancel_pending_order"), tool(), agent(ask), user("Yes.###STOP###")
        )
        assert check_premature_stop(messages).premature_stop, ask


def test_offer_after_the_write_is_not_a_pending_confirmation() -> None:
    # As in pilot1 task 1 trial 2 (reward 1.0): the exchange ran, then the agent offered
    # a link and the customer said yes and stopped. Nothing the task needs is pending.
    done = "Your exchange is done. "
    for ask, answer in (
        ("Would you like me to generate that link?", "Yes, please send me the link.###STOP###"),
        (done + "Would you like me to email you a receipt?", "Yes please###STOP###"),
        (done + ASK, "Yes.###STOP###"),  # the sentence with the cue names no write
    ):
        messages = convo(
            agent(None, "exchange_delivered_order_items"), tool(), agent(ask), user(answer)
        )
        check = check_premature_stop(messages)
        assert not check.premature_stop and not check.stop_after_unexecuted_confirmation, ask


def test_custom_write_tools() -> None:
    messages = convo(agent(ASK, "my_write"), user("Yes.###STOP###"))
    assert check_premature_stop(messages).premature_stop  # unknown tool: not a write
    assert not check_premature_stop(messages, write_tools={"my_write"}).premature_stop


def test_stop_without_preceding_agent_text_is_not_flagged() -> None:
    # Two customer messages in a row: the STOP answers no agent message.
    messages = convo(agent(ASK), user("One moment."), user("Yes.###STOP###"))
    assert not check_premature_stop(messages).stop_after_unexecuted_confirmation


def test_textless_customer_tool_call_is_skipped() -> None:
    # Telecom-style user tool call between the agent request and the customer's STOP.
    messages = convo(agent(ASK), user(None, "toggle_airplane_mode"), tool(), user("yes ###STOP###"))
    assert check_premature_stop(messages).premature_stop


def test_no_stop_at_all() -> None:
    check = check_premature_stop(convo(agent(ASK), user("Yes.")))
    assert check == fidelity.StopCheck()


def test_first_qualifying_stop_is_reported() -> None:
    messages = convo(
        agent(SIGN_OFF), user("###STOP###"), agent(ASK), user("Yes.###STOP###"), agent("Bye")
    )
    check = check_premature_stop(messages)
    assert check.premature_stop and check.stop_index == 7


# ----------------------------------------------------------------- transfer requests


@pytest.mark.parametrize(
    ("text", "cue"),
    [
        ("Can I speak with a human agent?", "speak with a human"),
        ("I want to talk to a real person", "talk to a real person"),
        ("Please transfer me to a supervisor.", "transfer me"),
        ("kisi insaan se baat karni hai", "kisi insaan se baat"),
        ("मुझे किसी इंसान से बात करनी है", "इंसान"),
    ],
)
def test_transfer_cue_positive(text: str, cue: str) -> None:
    assert transfer_cue(text) == cue


@pytest.mark.parametrize("text", ["###TRANSFER###", "Thanks, that's all.", ""])
def test_transfer_cue_negative(text: str) -> None:
    assert transfer_cue(text) is None


@pytest.mark.parametrize(
    "supervisor",
    [
        "सुपरवाइज़र",  # precomposed ज़ (U+095B)
        "सुपरवाइज़र",  # ज + nukta
        "सुपरवाइजर",  # spelled without nukta
    ],
)
def test_transfer_cue_supervisor_with_and_without_nukta(supervisor: str) -> None:
    cue = transfer_cue(f"मुझे {supervisor} से बात करनी है")
    assert cue is not None and cue.startswith("सुपरवाइ")


def test_transfer_columns() -> None:
    messages = convo(
        agent("I can help with that."),
        user("Can I talk to a human please?"),
        agent(None, "transfer_to_human_agents"),
        tool(),
        agent("You are being transferred."),
        user("###TRANSFER###"),
    )
    row = simulation_fidelity(sim(messages))
    assert row["transfer_requested_by_customer"] and row["agent_transferred"]
    assert row["customer_transfer_turns"] == 1
    assert row["end_token"] == "TRANSFER" and not row["stop_with_content"]


# ----------------------------------------------------------------- basics


def test_turn_counts_and_end_token() -> None:
    messages = convo(agent(SIGN_OFF), user("Thanks!###STOP###"))
    row = simulation_fidelity(sim(messages))
    # agent: greeting + sign-off (the tool-call-only message does not count)
    assert (row["customer_turns"], row["agent_turns"]) == (2, 2)
    assert row["end_token"] == "STOP" and row["stop_with_content"]
    bare = simulation_fidelity(sim(convo(agent(SIGN_OFF), user("###STOP###"))))
    assert bare["end_token"] == "STOP" and not bare["stop_with_content"]
    assert simulation_fidelity(sim([]))["customer_turns"] == 0


def test_message_text_list_content() -> None:
    msg = {"role": "user", "content": [{"type": "text", "text": "Yes."}, {"text": "###STOP###"}]}
    assert fidelity.message_text(msg) == "Yes. ###STOP###"


# ----------------------------------------------------------------- script leakage


def test_script_counts_skips_id_like_tokens() -> None:
    counts = script_counts("जी, mera order #W2378156 hai, email a@b.com user_1 19122 हाँ।")
    # Devanagari: जी (2) + हाँ (3, the candrabindu mark counts); "।" is punctuation.
    # Latin: mera, order, hai, email = 4 + 5 + 3 + 5.
    assert counts == {"devanagari": 5, "latin": 17, "other": 0}


def test_script_counts_word_stuck_to_a_special_token() -> None:
    # gpt-oss-120b often glues the token to the last word ("Yes.###STOP###").
    expected = {"devanagari": 7, "latin": 8, "other": 0}  # Theek hai (8) + धन्यवाद (7)
    for text in ("Theek hai, धन्यवाद###STOP###", "Theek hai, धन्यवाद ###STOP###"):
        assert script_counts(text) == expected, text
    assert script_counts("धन्यवाद###TRANSFER###")["devanagari"] == 7
    assert script_counts("ok###OUT-OF-SCOPE###")["latin"] == 2
    # a romanized run whose only Devanagari is that last word now leaks
    s = _hindi_sim("Mera order hai", "Theek hai, धन्यवाद###STOP###")
    row = simulation_fidelity(s, {**HINDI_INFO, "text_input_style": "romanized"})
    assert row["script_leakage"] is True and row["devanagari_letters"] == 7


@pytest.mark.parametrize(
    ("info", "task_id", "sim_extra", "expected"),
    [
        ({"user_persona_id": "hi"}, "0_hi", {}, "hi"),
        ({"user_persona_id": "rishika_hindi_v1"}, "0", {}, "hi"),
        ({"task_set_name": "retail_hi"}, "0", {}, "hi"),
        ({}, "0_hi", {}, "hi"),
        ({}, "0", {"speech_environment": {"language": "hi"}}, "hi"),
        ({"user_persona_id": "en"}, "0_en", {}, "en"),
        ({}, "17", {}, None),  # plain tau2 main run
    ],
)
def test_infer_user_language(
    info: dict[str, Any], task_id: str, sim_extra: dict[str, Any], expected: str | None
) -> None:
    assert infer_user_language(info, task_id, sim_extra) == expected


def test_leak_script_for() -> None:
    assert leak_script_for("hi", "romanized") == "devanagari"
    assert leak_script_for("hi", "code_mixed") == "devanagari"
    assert leak_script_for("hi", "native_script") == "latin"
    assert leak_script_for("hi", None) == "latin"  # branch default is native_script
    assert leak_script_for("hi", "diacritic_free") is None
    assert leak_script_for("en", "romanized") is None
    assert leak_script_for(None, None) is None


HINDI_INFO = {"user_persona_id": "hi", "task_set_name": "retail_hi"}


def _hindi_sim(*turns: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for t in turns:
        messages += [agent("नमस्ते"), user(t)]
    return sim(messages, task_id="0_hi")


def test_romanized_leakage() -> None:
    s = _hindi_sim("Mera order #W2378156 hai", "जी, haan", "theek hai ###STOP###")
    row = simulation_fidelity(s, {**HINDI_INFO, "text_input_style": "romanized"})
    assert row["leak_script"] == "devanagari" and row["script_leakage"] is True
    assert row["script_leak_turns"] == 1 == row["customer_turns_with_devanagari"]
    # letters: Mera order hai (12) + जी haan (2 + 4) + theek hai (8) = 26; Devanagari 2
    assert row["customer_letters"] == 26 and row["devanagari_letters"] == 2
    assert row["script_leak_share"] == pytest.approx(2 / 26)
    clean = simulation_fidelity(
        _hindi_sim("Mera order hai", "ji haan"), {**HINDI_INFO, "text_input_style": "code_mixed"}
    )
    assert clean["script_leakage"] is False and clean["script_leak_share"] == 0.0


def test_native_script_leakage_uses_latin_share() -> None:
    mostly_latin = _hindi_sim("Mera order cancel kar dijiye please", "हाँ")
    mostly_native = _hindi_sim("मेरा ऑर्डर कैंसल कर दीजिए", "हाँ, Google Home")
    info = {**HINDI_INFO, "text_input_style": "native_script"}
    leaky = simulation_fidelity(mostly_latin, info)
    assert leaky["leak_script"] == "latin" and leaky["script_leakage"] is True
    ok = simulation_fidelity(mostly_native, info)
    assert ok["script_leakage"] is False and 0 < ok["latin_share"] < 0.5
    stricter = simulation_fidelity(mostly_native, info, native_latin_share_max=0.1)
    assert stricter["script_leakage"] is True


def test_english_run_has_no_leakage_flag() -> None:
    row = simulation_fidelity(sim(convo(agent(ASK), user("Yes.###STOP###"))), {})
    assert row["leak_script"] is None and row["script_leakage"] is pd.NA
    assert row["script_leak_turns"] is pd.NA and math.isnan(row["script_leak_share"])
    assert row["devanagari_letters"] == 0 and row["latin_share"] == 1.0


# ----------------------------------------------------------------- real transcripts


def _by_key(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.set_index(["task_id", "trial"])


def test_real_gptoss_audit_cases_are_flagged() -> None:
    f = _by_key(fidelity_frame(GPTOSS))
    audit = [("18", 0), ("9", 1), ("32", 0)]  # RQ1 chunk 1 failure audit, "Yes ... ###STOP###"
    for key in audit:
        row = f.loc[key]
        assert row["premature_stop"] and row["stop_after_unexecuted_confirmation"], key
        assert row["affirmation_cue"] == "yes"
    assert f.loc[("18", 0), "stop_message"] == "Yes.###STOP###"
    assert f.loc[("18", 0), "confirmation_cue"] == "proceed"
    others = f.drop(index=audit)
    assert not others["premature_stop"].any()
    assert not others["stop_after_unexecuted_confirmation"].any()


def test_real_gptoss_other_columns() -> None:
    f = _by_key(fidelity_frame(GPTOSS))
    # 12/1: the customer asks for a human, the agent transfers, the customer ends with TRANSFER.
    row = f.loc[("12", 1)]
    assert row["transfer_requested_by_customer"] and row["agent_transferred"]
    assert row["transfer_cue"] == "speak with a human" and row["end_token"] == "TRANSFER"
    # 17/2 and 27/0 end with a bare STOP; 18/1 with "That works for me. ###STOP###".
    assert not f.loc[("17", 2), "stop_with_content"] and not f.loc[("27", 0), "stop_with_content"]
    assert f.loc[("18", 1), "stop_with_content"]
    assert f["text_input_style"].isna().all() and f["leak_script"].isna().all()
    assert f["script_leakage"].isna().all()
    assert (f["customer_turns"] > 0).all() and (f["agent_turns"] > 0).all()


def test_real_qwen38_is_not_flagged() -> None:
    f = _by_key(fidelity_frame(QWEN38))
    assert not f["premature_stop"].any() and not f["stop_after_unexecuted_confirmation"].any()
    assert f.loc[("32", 0), "transfer_requested_by_customer"]
    assert f.loc[("32", 0), "transfer_cue"] == "transfer me"
    assert not f.loc[("9", 0), "transfer_requested_by_customer"]


def test_real_hindi_romanized_leakage() -> None:
    row = fidelity_frame(HINDI).iloc[0]
    assert (row["task_id"], row["text_input_style"], row["user_language"]) == (
        "0_hi",
        "romanized",
        "hi",
    )
    assert (row["customer_turns"], row["agent_turns"]) == (8, 8)
    # Matches the smoke-test note: 7 of 8 customer turns contain Devanagari.
    assert row["customer_turns_with_devanagari"] == 7 == row["script_leak_turns"]
    assert (row["devanagari_letters"], row["latin_letters"]) == (69, 1229)
    assert row["script_leak_share"] == pytest.approx(69 / 1298)
    assert bool(row["script_leakage"])
    # The customer said "जी, हाँ" to the agent's Devanagari request but without STOP;
    # the agent then executed the exchange, so nothing is premature.
    assert not row["premature_stop"] and not row["stop_after_unexecuted_confirmation"]


def test_real_hindi_confirmation_exchange() -> None:
    data = json.loads((HINDI / "results.json").read_text(encoding="utf-8"))
    messages = data["simulations"][0]["messages"]
    idx = next(
        i for i, m in enumerate(messages) if m["role"] == "user" and "हाँ" in (m["content"] or "")
    )
    asked = fidelity._preceding_agent_text(messages, idx)
    assert asked is not None
    assert confirmation_cue(messages[asked]["content"]) == '"हाँ"'
    assert affirmation_cue(messages[idx]["content"]) == "जी, हाँ"


def test_real_post_write_bullet_is_not_a_confirmation() -> None:
    # gpt-oss-120b task 55 trial 0 (fail): all 4 writes ran, then the agent listed what the
    # customer will be able to do ("- Confirm it's received by us") and the customer
    # thanked and stopped. It used to be a loose-flag false positive.
    row = fidelity_frame(GPTOSS_POST_WRITE).set_index(["task_id", "trial"]).loc[("55", 0)]
    assert not row["premature_stop"] and not row["stop_after_unexecuted_confirmation"]
    assert row["confirmation_cue"] is None and row["end_token"] == "STOP"


def test_real_offer_after_the_write_is_not_premature() -> None:
    # pilot1 task 1 trial 2 (reward 1.0): exchange_delivered_order_items ran, then the
    # agent offered a link ("Would you like me to generate that link for you?") and the
    # customer said "Yes, please send me the direct link ... ###STOP###". It used to be
    # a strict false positive on a passing simulation.
    data = json.loads((PILOT_LINK / "results.json").read_text(encoding="utf-8"))
    sim_1_2 = data["simulations"][0]
    assert (sim_1_2["task_id"], sim_1_2["trial"]) == ("1", 2)
    messages = sim_1_2["messages"]
    asked = next(
        i for i, m in enumerate(messages) if "generate that link" in (m.get("content") or "")
    )
    assert confirmation_cue(messages[asked]["content"]) == "would you like me to"
    row = fidelity_frame(PILOT_LINK).iloc[0]
    assert not row["premature_stop"] and not row["stop_after_unexecuted_confirmation"]
    summary = fidelity_summary(add_fidelity(load_runs(PILOT_LINK))).set_index("outcome")
    assert summary.loc["pass", "premature_stop"] == 0


SECRET_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:gsk_|sk-|sk_|AIza|hf_|xai-|ghp_|github_pat_)[A-Za-z0-9_\-]{8,}"
    r"|org_(?!REDACTED)[A-Za-z0-9]+"
)


@pytest.mark.parametrize("run", [GPTOSS, QWEN38, HINDI, GPTOSS_POST_WRITE, PILOT_LINK])
def test_fixtures_hold_no_secrets(run: Path) -> None:
    text = (run / "results.json").read_text(encoding="utf-8")
    assert not SECRET_RE.findall(text)
    keys = re.findall(r'"api_key"\s*:\s*"([^"]*)"', text)
    assert set(keys) <= {"REDACTED"}


# ----------------------------------------------------------------- frames and summary


def test_add_fidelity_joins_onto_load_runs() -> None:
    df = load_runs([GPTOSS, QWEN38, HINDI])
    out = add_fidelity(df)
    assert len(out) == len(df) == 13
    assert list(out.columns[: len(df.columns)]) == list(df.columns)
    assert set(FIDELITY_COLUMNS) <= set(out.columns)
    # Row order and keys of the loaded frame are kept (load_runs sorts by task id).
    pd.testing.assert_frame_equal(out[df.columns], df)
    flagged = out[out["premature_stop"]]
    assert sorted(zip(flagged["task_id"], flagged["trial"], strict=True)) == [
        ("18", 0),
        ("32", 0),
        ("9", 1),
    ]
    assert out["run_name"][out["premature_stop"]].unique().tolist() == ["fid_rq1_gptoss120b"]
    # Calling it again replaces the columns instead of duplicating them.
    again = add_fidelity(out)
    assert list(again.columns) == list(out.columns)


def test_add_fidelity_duplicates_and_missing_rows(tmp_path: Path) -> None:
    ask = sim(convo(agent(ASK), user("Yes.###STOP###")), task_id="1", trial=0, reward=0.0)
    fine = sim(convo(agent(SIGN_OFF), user("###STOP###")), task_id="1", trial=0, reward=1.0)
    run = write_run(tmp_path / "dup", [ask, fine])  # (1, 0) twice, in this order
    df = load_runs(run, on_duplicates="ignore")
    out = add_fidelity(df)
    # The two rows are matched in file order: reward 0 <-> premature, reward 1 <-> not.
    assert out.sort_values("reward")["premature_stop"].tolist() == [True, False]

    # A loaded row that is no longer in the file gets missing values and is reported.
    ghost = df.iloc[[0]].assign(trial=5, success=pd.array([False], dtype="boolean"))
    out = add_fidelity(pd.concat([df, ghost], ignore_index=True))
    assert pd.isna(out.iloc[-1]["customer_turns"])
    summary = fidelity_summary(out).set_index("outcome")
    assert summary.loc["all", "sims"] == 2 and summary.loc["all", "unmatched"] == 1
    assert summary.loc["fail", "unmatched"] == 1 and summary.loc["pass", "unmatched"] == 0
    assert "WARNING: 1 valid simulation(s) had no transcript" in format_fidelity(out)


def test_add_fidelity_empty_frame() -> None:
    empty = load_runs(GPTOSS).iloc[0:0]
    out = add_fidelity(empty)
    assert out.empty and set(FIDELITY_COLUMNS) <= set(out.columns)


def test_fidelity_summary_by_outcome(tmp_path: Path) -> None:
    sims = [
        sim(convo(agent(ASK), user("Yes.###STOP###")), task_id="1", trial=0, reward=0.0),
        sim(convo(agent(ASK), user("###STOP###")), task_id="1", trial=1, reward=0.0),
        sim(convo(agent(SIGN_OFF), user("Thanks!###STOP###")), task_id="2", trial=0),
        sim(convo(user("Can I speak to a human?"), user("###TRANSFER###")), task_id="2", trial=1),
        sim([], task_id="3", trial=0, reward=None),  # infra error: excluded
    ]
    df = add_fidelity(load_runs(write_run(tmp_path / "synth", sims)))
    s = fidelity_summary(df).set_index("outcome")
    assert s.loc["pass", "sims"] == 2 and s.loc["fail", "sims"] == 2 and s.loc["all", "sims"] == 4
    assert s.loc["fail", "premature_stop"] == 1 and s.loc["fail", "premature_stop_rate"] == 0.5
    assert s.loc["fail", "stop_after_unexecuted_confirmation"] == 2
    assert s.loc["pass", "premature_stop"] == 0
    assert s.loc["pass", "transfer_requested_by_customer"] == 1
    assert s.loc["all", "transfer_requested_by_customer_rate"] == pytest.approx(0.25)
    assert s.loc["all", "script_leak_applicable"] == 0
    assert math.isnan(s.loc["all", "script_leakage_rate"])

    text = format_fidelity(df)
    assert "fail     2       1 (0.50)                  2 (1.00)" in text
    assert "premature_stop: task 1 trial 0 (fail)" in text
    assert "transfer_requested_by_customer: task 2 trial 1 (pass)" in text
    assert "task 3" not in text  # infra error


def test_fidelity_summary_hindi_rates() -> None:
    df = add_fidelity(load_runs(HINDI))
    s = fidelity_summary(df).set_index("outcome")
    assert s.loc["pass", "script_leak_applicable"] == 1 and s.loc["pass", "script_leakage"] == 1
    assert s.loc["pass", "mean_script_leak_share"] == pytest.approx(69 / 1298)
    assert s.loc["fail", "sims"] == 0 and math.isnan(s.loc["fail", "script_leakage_rate"])
    text = format_fidelity(df)
    assert "text_input_style: romanized; leak script: devanagari" in text
    assert text.isascii()  # safe for a cp1252 Windows console


# ----------------------------------------------------------------- analyze integration


def test_analyze_report_without_fidelity_is_unchanged() -> None:
    df = load_runs(GPTOSS)
    assert analyze.report(df) == analyze.report(df, fidelity=False)
    assert "-- simulator fidelity --" not in analyze.report(df)


def test_analyze_report_with_fidelity_adds_section() -> None:
    df = load_runs([GPTOSS, QWEN38])
    text = analyze.report(df, fidelity=True)
    assert text.count("-- simulator fidelity --") == 2
    assert (
        "premature_stop: task 9 trial 1 (fail), task 18 trial 0 (fail), task 32 trial 0 (fail)"
        in text
    )
    # The section comes after the failed-simulations table of its own run.
    gptoss_block = text.split("=== run: ")[1]
    assert gptoss_block.index("-- failed simulations --") < gptoss_block.index(
        "-- simulator fidelity --"
    )
    # Passing an already-enriched frame gives the same text.
    assert analyze.report(add_fidelity(df), fidelity=True) == text


def test_analyze_cli_fidelity_and_csv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    csv_path = tmp_path / "fid.csv"
    assert analyze.main([str(GPTOSS), str(HINDI), "--fidelity", "--csv", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert out.count("-- simulator fidelity --") == 2
    assert "leak script: devanagari" in out
    written = pd.read_csv(csv_path, dtype={"task_id": str}, encoding="utf-8")
    assert len(written) == 10 and "premature_stop" in written.columns
    assert int(written["premature_stop"].sum()) == 3
    hindi = written[written["task_id"] == "0_hi"].iloc[0]
    assert hindi["customer_turns_with_devanagari"] == 7

    plain_csv = tmp_path / "plain.csv"
    assert analyze.main([str(GPTOSS), "--csv", str(plain_csv)]) == 0
    assert "-- simulator fidelity --" not in capsys.readouterr().out
    assert "premature_stop" not in pd.read_csv(plain_csv).columns
