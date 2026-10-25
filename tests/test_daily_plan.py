"""Daily plans use invented identities and messages; no private inputs are read."""
import copy
import itertools
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.config import ConfigError
from app.daily_plan import daily_choice, merge_private_targets
from app.models import Message


@pytest.fixture
def raw():
    return {
        "task_id": "fake-daily-task",
        "timezone": "Asia/Shanghai",
        "prevent_duplicates": True,
        "send_interval_seconds": {"min": 3, "max": 8},
        "target_open_timeout_seconds": 20,
        "targets": [{
            "name": "虚构好友甲",
            "sec_uid": "fake_uid_alpha",
            "messages": [{"type": "text", "value": "原来的虚构消息"}],
            "private_target_option": {"preserve": True},
        }],
    }


@pytest.fixture
def updates():
    return {
        "version": 1,
        "expected_total": 5,
        "add_targets": [
            {"name": "虚构好友乙", "sec_uid": "fake_uid_bravo"},
            {"name": "虚构好友丙", "sec_uid": "fake_uid_charlie"},
            {"name": "虚构好友丁", "sec_uid": "fake_uid_delta"},
            {"name": "虚构好友戊", "sec_uid": "fake_uid_echo"},
        ],
        "daily_messages": ["虚构的早安消息", "虚构的第二条问候", "虚构的第三条问候"],
    }


def message_texts(target):
    assert all(message["type"] == "text" for message in target["messages"])
    return [message.get("content", message.get("value")) for message in target["messages"]]


def without_scaffolding(text):
    return text.strip(" \t\r\n\u200b\ufeff")


@pytest.mark.parametrize("no_updates", [None, ""])
def test_missing_update_preserves_existing_configuration(raw, no_updates):
    before = copy.deepcopy(raw)
    assert merge_private_targets(raw, no_updates) == before
    assert raw == before


def test_four_additions_make_five_targets_and_replace_every_message_pool_without_mutation(raw, updates):
    before_raw, before_updates = copy.deepcopy(raw), copy.deepcopy(updates)
    result = merge_private_targets(raw, json.dumps(updates, ensure_ascii=False))
    assert len(result["targets"]) == 5
    assert [target["name"] for target in result["targets"]] == [
        raw["targets"][0]["name"], *[target["name"] for target in updates["add_targets"]],
    ]
    assert len({target["sec_uid"] for target in result["targets"]}) == 5
    assert all(message_texts(target) == updates["daily_messages"] for target in result["targets"])
    assert result["daily_message_rotation"] is True
    assert result["targets"][0]["private_target_option"] == {"preserve": True}
    for key in raw.keys() - {"targets"}:
        assert result[key] == raw[key]
    assert raw == before_raw and updates == before_updates


def test_exact_identity_repetitions_are_deduplicated_preserving_first_order(raw, updates):
    first = {"name": raw["targets"][0]["name"], "sec_uid": raw["targets"][0]["sec_uid"]}
    second = copy.deepcopy(updates["add_targets"][0])
    updates["add_targets"] = [first, second, copy.deepcopy(second), first]
    updates["expected_total"] = 2
    result = merge_private_targets(raw, json.dumps(updates))
    assert [(target["name"], target["sec_uid"]) for target in result["targets"]] == [
        (first["name"], first["sec_uid"]), (second["name"], second["sec_uid"]),
    ]


def test_existing_target_order_and_parameters_survive_updates(raw, updates):
    raw["targets"].append({
        "name": "虚构的既有第二位", "sec_uid": "fake_uid_existing_second",
        "messages": [{"type": "text", "content": "旧消息"}], "custom_setting": [1, 2, 3],
    })
    updates["expected_total"] = 3
    updates["add_targets"] = updates["add_targets"][:1]
    before = copy.deepcopy(raw)
    result = merge_private_targets(raw, json.dumps(updates))
    assert [target["sec_uid"] for target in result["targets"]] == [
        "fake_uid_alpha", "fake_uid_existing_second", "fake_uid_bravo",
    ]
    assert result["targets"][1]["custom_setting"] == [1, 2, 3]
    assert raw == before


def test_empty_additions_can_enable_rotation_for_existing_fixed_targets(raw, updates):
    updates.update(expected_total=1, add_targets=[])
    result = merge_private_targets(raw, json.dumps(updates))
    assert len(result["targets"]) == 1
    assert result["daily_message_rotation"] is True
    assert message_texts(result["targets"][0]) == updates["daily_messages"]


@pytest.mark.parametrize("payload", ["not-json", "[]", "null", "true", "42", '"text"'])
def test_update_must_be_a_json_object(raw, payload):
    with pytest.raises(ConfigError):
        merge_private_targets(raw, payload)


@pytest.mark.parametrize("key", ["version", "expected_total", "add_targets", "daily_messages"])
def test_update_requires_every_contract_field(raw, updates, key):
    del updates[key]
    with pytest.raises(ConfigError):
        merge_private_targets(raw, json.dumps(updates))


def test_unknown_top_level_fields_are_rejected(raw, updates):
    updates["prevent_duplicates"] = False
    with pytest.raises(ConfigError):
        merge_private_targets(raw, json.dumps(updates))


@pytest.mark.parametrize("version", [True, False, 0, 2, "1", 1.0, None])
def test_version_is_exact_integer_one(raw, updates, version):
    updates["version"] = version
    with pytest.raises(ConfigError):
        merge_private_targets(raw, json.dumps(updates))


@pytest.mark.parametrize("total", [True, False, 0, 11, "5", 5.0, None, 4, 6])
def test_expected_total_is_a_bounded_integer_matching_merged_count(raw, updates, total):
    updates["expected_total"] = total
    with pytest.raises(ConfigError):
        merge_private_targets(raw, json.dumps(updates))


@pytest.mark.parametrize("replacement", [
    {"name": "虚构好友甲", "sec_uid": "fake_uid_different"},
    {"name": "不同的虚构昵称", "sec_uid": "fake_uid_alpha"},
])
def test_name_or_uid_conflict_with_original_target_is_rejected(raw, updates, replacement):
    updates.update(expected_total=2, add_targets=[replacement])
    before = copy.deepcopy(raw)
    with pytest.raises(ConfigError):
        merge_private_targets(raw, json.dumps(updates))
    assert raw == before


@pytest.mark.parametrize("second", [
    {"name": "虚构新增甲", "sec_uid": "fake_uid_other"},
    {"name": "虚构新增乙", "sec_uid": "fake_uid_added"},
])
def test_conflicts_between_new_targets_are_rejected(raw, updates, second):
    updates.update(expected_total=3, add_targets=[
        {"name": "虚构新增甲", "sec_uid": "fake_uid_added"}, second,
    ])
    with pytest.raises(ConfigError):
        merge_private_targets(raw, json.dumps(updates))


@pytest.mark.parametrize("target", [
    {}, {"name": "虚构好友"}, {"sec_uid": "fake_uid_added"},
    {"name": "", "sec_uid": "fake_uid_added"},
    {"name": "   ", "sec_uid": "fake_uid_added"},
    {"name": 42, "sec_uid": "fake_uid_added"},
    {"name": "虚构好友", "sec_uid": "short"},
    {"name": "虚构好友", "sec_uid": "PRIVATE_INVALID_UID?"},
    {"name": "虚构好友", "sec_uid": "fake_uid_added", "messages": []},
    "PRIVATE_INVALID_TARGET",
])
def test_new_target_requires_only_a_valid_name_and_fixed_uid(raw, updates, target):
    updates.update(expected_total=2, add_targets=[target])
    with pytest.raises(ConfigError) as error:
        merge_private_targets(raw, json.dumps(updates))
    assert "PRIVATE_" not in str(error.value)


@pytest.mark.parametrize("uid", [None, "", "short", "PRIVATE_INVALID_UID?"])
def test_original_target_must_already_have_a_valid_fixed_uid(raw, updates, uid):
    if uid is None:
        raw["targets"][0].pop("sec_uid")
    else:
        raw["targets"][0]["sec_uid"] = uid
    with pytest.raises(ConfigError) as error:
        merge_private_targets(raw, json.dumps(updates))
    assert "PRIVATE_" not in str(error.value)


@pytest.mark.parametrize("messages", [
    [], ["唯一文本"], ["相同文本", "相同文本"],
    ["相同文本", "\u200b相同文本\ufeff"],
    ["相同文本", "相同文本", "其他文本"],
    ["正常文本", " \u200b\ufeff\t"], ["正常文本", "x" * 201],
    ["正常文本", 42], ["正常文本", None], "不是消息数组",
])
def test_message_pool_requires_two_or_more_distinct_nonblank_short_texts(raw, updates, messages):
    updates["daily_messages"] = messages
    with pytest.raises(ConfigError):
        merge_private_targets(raw, json.dumps(updates))


def test_two_hundred_character_message_is_allowed(raw, updates):
    updates["daily_messages"] = ["x" * 200, "y" * 200]
    result = merge_private_targets(raw, json.dumps(updates))
    assert message_texts(result["targets"][0]) == updates["daily_messages"]


@pytest.fixture
def candidates():
    return tuple(Message(type="text", content=text) for text in ("虚构早安甲", "虚构早安乙", "虚构早安丙"))


def test_daily_choice_is_repeatable_and_adjacent_days_differ(candidates):
    start = date(2026, 12, 30)
    picks = []
    for offset in range(8):
        day = (start + timedelta(days=offset)).isoformat()
        chosen = daily_choice(candidates, "fake_uid_alpha", day)
        assert any(chosen is candidate for candidate in candidates)
        assert daily_choice(candidates, "fake_uid_alpha", day) is chosen
        picks.append(chosen.content)
    assert all(left != right for left, right in zip(picks, picks[1:]))
    assert len(set(picks[:len(candidates)])) == len(candidates)


def test_candidate_reordering_does_not_change_choice_or_mutate_input(candidates):
    original_order = tuple(candidates)
    expected = daily_choice(candidates, "fake_uid_alpha", "2026-10-25").content
    for permutation in itertools.permutations(candidates):
        supplied = list(permutation)
        before = supplied.copy()
        assert daily_choice(supplied, "fake_uid_alpha", "2026-10-25").content == expected
        assert supplied == before
    assert candidates == original_order


def test_two_message_pool_alternates_across_leap_day():
    choices = [SimpleNamespace(content="虚构短消息甲"), SimpleNamespace(content="虚构短消息乙")]
    days = ("2028-02-28", "2028-02-29", "2028-03-01")
    picks = [daily_choice(choices, "fake_uid_alpha", day).content for day in days]
    assert picks[0] != picks[1] and picks[1] != picks[2]
    assert picks[0] == picks[2]


def test_normalized_message_text_determines_stable_hash_order(candidates):
    wrapped = [SimpleNamespace(content=" \u200b" + candidate.content + "\ufeff ") for candidate in candidates]
    for day in ("2026-10-25", "2026-10-26", "2026-10-27"):
        plain = daily_choice(candidates, "fake_uid_alpha", day)
        with_scaffolding = daily_choice(list(reversed(wrapped)), "fake_uid_alpha", day)
        assert without_scaffolding(with_scaffolding.content) == plain.content


@pytest.mark.parametrize("day", [None, "", "2026-02-30", "2026-1-2", "2026-10-25T07:30:00", "not-date"])
def test_daily_choice_rejects_invalid_or_noncanonical_dates(candidates, day):
    with pytest.raises(ConfigError):
        daily_choice(candidates, "fake_uid_alpha", day)


@pytest.mark.parametrize("uid", [None, "", "short", "PRIVATE_INVALID_UID?", 42])
def test_daily_choice_requires_a_valid_fixed_uid(candidates, uid):
    with pytest.raises(ConfigError) as error:
        daily_choice(candidates, uid, "2026-10-25")
    assert "PRIVATE_" not in str(error.value)


@pytest.mark.parametrize("texts", [
    [], ["单条文本"], ["重复文本", "重复文本"],
    ["重复文本", "\u200b重复文本\ufeff"], ["正常文本", " \u200b\ufeff"],
    ["正常文本", "x" * 201], ["正常文本", None],
])
def test_daily_choice_rejects_invalid_or_duplicate_text_candidates(texts):
    choices = [SimpleNamespace(content=text) for text in texts]
    with pytest.raises(ConfigError):
        daily_choice(choices, "fake_uid_alpha", "2026-10-25")
