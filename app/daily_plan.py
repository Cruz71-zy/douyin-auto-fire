"""Private recipient extensions and reproducible day-by-day message rotation."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date

from app.config import ConfigError
from app.safe_sender import editor_text


def _identity(target, *, extension=False):
    if not isinstance(target, dict):
        raise ConfigError("私密好友配置格式无效")
    kind = target.get("kind", "user")
    allowed = ({"name", "kind", "group_id", "group_short_id"} if kind == "group"
               else {"name", "sec_uid", "kind"} if "kind" in target else {"name", "sec_uid"})
    if kind not in {"user", "group"} or (extension and set(target) != allowed):
        raise ConfigError("私密目标配置格式无效")
    name = target.get("name")
    uid = target.get("sec_uid")
    if (not isinstance(name, str) or not name.strip() or len(name) > 100
            or kind == "user" and (not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,200}", uid))):
        raise ConfigError("私密好友必须包含已核验的昵称和固定标识")
    if kind == "group":
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,200}", value)
               for value in (target.get("group_id"), target.get("group_short_id"))):
            raise ConfigError("群聊必须包含已核验的固定会话标识")
        uid = "group:" + target["group_id"] + ":" + target["group_short_id"]
    else:
        uid = "user:" + uid
    return name.strip(), uid


def _message_text(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise ConfigError("轮换消息必须为一至二百字的文字")
    normalized = editor_text(value)
    if not normalized:
        raise ConfigError("轮换消息不能为空")
    return normalized


def merge_private_targets(raw: dict, updates_json: str | None) -> dict:
    if not updates_json:
        return copy.deepcopy(raw)
    try:
        updates = json.loads(updates_json)
    except (TypeError, ValueError):
        raise ConfigError("私密好友扩展不是有效 JSON") from None
    if (not isinstance(raw, dict) or not isinstance(updates, dict)
            or set(updates) != {"version", "expected_total", "add_targets", "daily_messages"}
            or type(updates["version"]) is not int or updates["version"] != 1
            or type(updates["expected_total"]) is not int
            or not 1 <= updates["expected_total"] <= 10
            or not isinstance(updates["add_targets"], list)
            or len(updates["add_targets"]) > 10
            or not isinstance(updates["daily_messages"], list)
            or not 2 <= len(updates["daily_messages"]) <= 100):
        raise ConfigError("私密好友扩展的结构或人数无效")
    messages = [_message_text(value) for value in updates["daily_messages"]]
    if len(set(messages)) != len(messages):
        raise ConfigError("轮换消息不能包含相同或仅占位字符不同的文字")
    combined = copy.deepcopy(raw)
    targets = combined.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ConfigError("原配置必须已经固定好友身份")
    names, uids = {}, {}
    for index, target in enumerate(targets):
        name, uid = _identity(target)
        if name in names or uid in uids:
            raise ConfigError("原配置包含重复好友")
        target["name"] = name
        names[name], uids[uid] = index, index
    added_names, added_uids = {}, {}
    for target in updates["add_targets"]:
        name, uid = _identity(target, extension=True)
        if name in added_names or uid in added_uids:
            if added_names.get(name) == uid and added_uids.get(uid) == name:
                continue
            raise ConfigError("新增好友的固定身份冲突")
        added_names[name], added_uids[uid] = uid, name
        if name in names or uid in uids:
            if name in names and uid in uids and names[name] == uids[uid]:
                continue
            raise ConfigError("新增好友与原配置的固定身份冲突")
        names[name], uids[uid] = len(targets), len(targets)
        addition = copy.deepcopy(target)
        addition["name"] = name
        targets.append(addition)
    if len(targets) != updates["expected_total"]:
        raise ConfigError("合并后的好友人数与明确指定的人数不符")
    for target in targets:
        target["messages"] = [{"type": "text", "content": text} for text in messages]
    combined["daily_message_rotation"] = True
    return combined


def rotation_identity(kind, identity):
    if kind == "group":
        return "group_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return identity


def daily_choice(candidates, uid: str, day: str):
    if not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,200}", uid):
        raise ConfigError("消息轮换需要已核验的固定身份")
    try:
        parsed = date.fromisoformat(day)
        if parsed.isoformat() != day:
            raise ValueError
    except (TypeError, ValueError):
        raise ConfigError("消息轮换日期无效") from None
    options = list(candidates)
    if len(options) < 2:
        raise ConfigError("消息轮换至少需要两条不同文字")
    ranked, seen = [], set()
    for message in options:
        if getattr(message, "type", "text") != "text":
            raise ConfigError("消息轮换仅支持文字")
        text = _message_text(getattr(message, "content", None))
        if text in seen:
            raise ConfigError("消息轮换不能包含重复文字")
        seen.add(text)
        digest = hashlib.sha256((uid + "\0" + text).encode("utf-8")).digest()
        ranked.append((digest, message))
    ranked.sort(key=lambda item: item[0])
    return ranked[parsed.toordinal() % len(ranked)][1]
