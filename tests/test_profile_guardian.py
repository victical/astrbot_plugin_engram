import pytest

from astrbot_plugin_engram.services.profile_guardian import ProfileGuardian


def test_confidence_promotion():
    guardian = ProfileGuardian({"enable_profile_confidence": True, "profile_confidence_threshold": 2})

    current_profile = {
        "attributes": {"hobbies": []},
        "pending_proposals": [],
    }

    new_llm_profile = {
        "attributes": {"hobbies": ["跳伞"]},
    }

    validated_1, _ = guardian.validate_update(current_profile, new_llm_profile, "")
    assert "跳伞" not in validated_1["attributes"]["hobbies"]
    assert len(validated_1["pending_proposals"]) == 1
    assert validated_1["pending_proposals"][0]["confidence"] == 1

    current_profile = validated_1

    validated_2, _ = guardian.validate_update(current_profile, new_llm_profile, "")
    assert "跳伞" in validated_2["attributes"]["hobbies"]
    assert len(validated_2["pending_proposals"]) == 0


@pytest.mark.parametrize(
    "text",
    [
        "我是男",
        "我是女",
        "我是男生",
        "我是女生",
        "我是男孩子",
        "我是女孩子",
        "我是男人",
        "我是女人",
        "我是男的",
        "我是个男",
        "我是个女",
        "我的性别是男",
        "性别为女",
    ],
)
def test_strong_evidence_gender_positive(text):
    guardian = ProfileGuardian()
    assert guardian._check_strong_evidence("gender", text)


@pytest.mark.parametrize(
    "text",
    [
        "我是男朋友",
        "我是女朋友",
        "我是男神",
        "我是女神",
        "我是男票",
        "我是女票",
        "我的男朋友",
        "她是女神",
    ],
)
def test_strong_evidence_gender_negative(text):
    guardian = ProfileGuardian()
    assert not guardian._check_strong_evidence("gender", text)


@pytest.mark.parametrize(
    "text",
    [
        "我18岁",
        "我18岁了",
        "今年20岁",
        "出生于1990年",
        "生日是1990-01-01",
        "生日1990年1月1日",
    ],
)
def test_strong_evidence_age_positive(text):
    guardian = ProfileGuardian()
    assert guardian._check_strong_evidence("age", text)


@pytest.mark.parametrize(
    "text",
    [
        "我十八岁",
        "今年二十岁",
        "年龄未知",
    ],
)
def test_strong_evidence_age_negative(text):
    guardian = ProfileGuardian()
    assert not guardian._check_strong_evidence("age", text)


@pytest.mark.parametrize(
    "text",
    [
        "我在北京工作",
        "我在上海生活",
        "我在广州上学",
        "我在南京读书",
        "我家在杭州",
        "我家在成都生活",
        "住在深圳市",
        "住在海淀区",
        "我是广州人",
        "我是北京本地人",
        "来自湖北省",
        "来自成都市",
    ],
)
def test_strong_evidence_location_positive(text):
    guardian = ProfileGuardian()
    assert guardian._check_strong_evidence("location", text)


@pytest.mark.parametrize(
    "text",
    [
        "我是程序员",
        "我是学生",
        "我是高级工程师",
        "我是医生",
        "我是律师",
        "我在医院工作",
        "我在公司上班",
        "我的职业是律师",
        "我做设计工作",
        "我是做IT的",
        "当英语老师",
    ],
)
def test_strong_evidence_job_positive(text):
    guardian = ProfileGuardian()
    assert guardian._check_strong_evidence("job", text)


def test_protect_basic_info_blocks_without_evidence():
    guardian = ProfileGuardian({"enable_strong_evidence_protection": True})

    old_basic = {"gender": "男", "age": "18", "nickname": "旧昵称"}
    new_basic = {"gender": "女", "age": "19", "nickname": "新昵称"}

    result = guardian._protect_basic_info(old_basic, new_basic, "没有明确陈述")

    assert result["gender"] == "男"
    assert result["age"] == "18"
    assert result["nickname"] == "旧昵称"


def test_protect_basic_info_allows_with_evidence():
    guardian = ProfileGuardian({"enable_strong_evidence_protection": True})

    old_basic = {"gender": "男", "job": "学生"}
    new_basic = {"gender": "女", "job": "程序员"}

    result = guardian._protect_basic_info(old_basic, new_basic, "我是女生，我是程序员")

    assert result["gender"] == "女"
    assert result["job"] == "程序员"


def test_conflict_detection_blocks_new_value():
    guardian = ProfileGuardian({"enable_conflict_detection": True})

    current_profile = {
        "preferences": {"likes": ["喜欢猫"], "dislikes": ["讨厌辣"]},
    }
    new_profile = {
        "preferences": {"likes": ["讨厌猫"], "dislikes": ["喜欢辣"]},
    }

    validated, conflicts = guardian.validate_update(current_profile, new_profile, "")

    assert "喜欢猫" in set(validated["preferences"]["likes"])
    assert "讨厌猫" not in set(validated["preferences"]["likes"])
    assert "讨厌辣" in set(validated["preferences"]["dislikes"])
    assert "喜欢辣" not in set(validated["preferences"]["dislikes"])
    assert any(c["conflict_type"] == "sentiment_conflict" for c in conflicts)


def test_allergy_conflict_blocks_new_value():
    guardian = ProfileGuardian({"enable_conflict_detection": True})

    current_profile = {
        "preferences": {"likes": ["猫毛过敏"]},
    }
    new_profile = {
        "preferences": {"likes": ["喜欢猫"]},
    }

    validated, conflicts = guardian.validate_update(current_profile, new_profile, "")

    assert "猫毛过敏" in set(validated["preferences"]["likes"])
    assert "喜欢猫" not in set(validated["preferences"]["likes"])
    assert any(c["conflict_type"] == "allergy_conflict" for c in conflicts)
