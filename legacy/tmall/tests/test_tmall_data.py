"""天猫数据落地契约的测试。

全部是纯函数测试：不连数据库、不读 ZIP、不碰文件系统。
ZIP 与数据库相关的行为由 scripts 层的测试覆盖。
"""

from datetime import date
from decimal import Decimal

import pytest

from app.services.tmall_data import (
    ACTION_TYPES,
    ERROR_ACTION_TYPE,
    ERROR_DATE,
    ERROR_FIELD_TYPE,
    ERROR_HEADER,
    ERROR_MESSAGES,
    ERROR_ROW_SHAPE,
    ERROR_CATEGORIES,
    FILE_SPECS,
    FILE_SPECS_BY_KEY,
    FUNNEL_ORDER,
    SAMPLING_COLUMN,
    TmallDataError,
    is_sampled,
    iter_decoded_log_rows,
    normalize_header,
    parse_action_type,
    parse_optional_int,
    parse_repurchase_row,
    parse_required_int,
    parse_user_info_row,
    parse_user_log_date,
    parse_user_log_row,
    validate_header,
)

LOG_HEADER = ("user_id", "item_id", "cat_id", "seller_id", "brand_id", "time_stamp", "action_type")


# --------------------------------------------------------------------------
# 文件契约
# --------------------------------------------------------------------------


def test_four_files_are_registered_with_expected_headers():
    assert set(FILE_SPECS_BY_KEY) == {"user_info", "user_log", "train", "test"}
    assert FILE_SPECS_BY_KEY["user_info"].header == ("user_id", "age_range", "gender")
    assert FILE_SPECS_BY_KEY["user_log"].header == LOG_HEADER
    assert FILE_SPECS_BY_KEY["train"].header == ("user_id", "merchant_id", "label")
    assert FILE_SPECS_BY_KEY["test"].header == ("user_id", "merchant_id", "prob")


def test_every_file_samples_on_the_same_column():
    """四个文件必须用同一列做用户级抽样，否则训练集的用户会在事件表里查不到。"""
    assert SAMPLING_COLUMN == "user_id"
    for spec in FILE_SPECS:
        assert SAMPLING_COLUMN in spec.header, spec.key


def test_train_and_test_write_into_the_same_silver_table():
    assert FILE_SPECS_BY_KEY["train"].table == "tmall_repurchase_samples"
    assert FILE_SPECS_BY_KEY["test"].table == "tmall_repurchase_samples"


def test_action_type_mapping_is_the_documented_one():
    assert ACTION_TYPES == {"0": "click", "1": "cart", "2": "buy", "3": "favorite"}
    assert FUNNEL_ORDER == ("click", "cart", "favorite", "buy")


# --------------------------------------------------------------------------
# 表头校验
# --------------------------------------------------------------------------


def test_normalize_header_strips_bom_and_case():
    assert normalize_header(["﻿User_ID", " Age_Range "]) == ("user_id", "age_range")


def test_validate_header_accepts_exact_match():
    validate_header(FILE_SPECS_BY_KEY["user_log"], LOG_HEADER)


def test_validate_header_accepts_bom_and_whitespace():
    validate_header(FILE_SPECS_BY_KEY["user_info"], ("﻿user_id", "age_range ", "gender"))


def test_validate_header_rejects_reordered_columns():
    """列顺序错了会让 item_id 被读成 cat_id，两者都是整数，静默出错。

    这条测试的意义就在于把「顺序」也纳入契约，而不只是「列名集合」。
    """
    reordered = ("user_id", "cat_id", "item_id", "seller_id", "brand_id", "time_stamp",
                 "action_type")
    with pytest.raises(TmallDataError) as excinfo:
        validate_header(FILE_SPECS_BY_KEY["user_log"], reordered)
    assert excinfo.value.category == ERROR_HEADER


def test_validate_header_rejects_renamed_column():
    """数据集换成 user_log_format2（列名不同）时必须在这里拦下，而不是导入到一半。"""
    with pytest.raises(TmallDataError) as excinfo:
        validate_header(FILE_SPECS_BY_KEY["user_log"], ("user_id",) + LOG_HEADER[1:-1] + ("action",))
    assert excinfo.value.category == ERROR_HEADER


def test_validate_header_rejects_missing_column():
    with pytest.raises(TmallDataError) as excinfo:
        validate_header(FILE_SPECS_BY_KEY["user_info"], ("user_id", "gender"))
    assert excinfo.value.category == ERROR_HEADER


# --------------------------------------------------------------------------
# 抽样
# --------------------------------------------------------------------------


def test_sampling_is_deterministic_and_user_level():
    assert is_sampled(0, 55, 0)
    assert is_sampled(55, 55, 0)
    assert is_sampled(110, 55, 0)
    assert not is_sampled(1, 55, 0)
    assert not is_sampled(54, 55, 0)


def test_sampling_residue_selects_a_different_partition():
    assert is_sampled(7, 55, 7)
    assert not is_sampled(7, 55, 0)


def test_sampling_rejects_invalid_parameters():
    with pytest.raises(ValueError):
        is_sampled(1, 0, 0)
    with pytest.raises(ValueError):
        is_sampled(1, 55, 55)
    with pytest.raises(ValueError):
        is_sampled(1, 55, -1)


def test_sampling_partitions_do_not_overlap_and_cover_everything():
    """1..110 每个用户必须恰好被一个余数选中——这是「稳定分区」的定义。"""
    for user_id in range(1, 111):
        hits = [residue for residue in range(55) if is_sampled(user_id, 55, residue)]
        assert len(hits) == 1


# --------------------------------------------------------------------------
# action_type
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", "click"), ("1", "cart"), ("2", "buy"), ("3", "favorite")],
)
def test_parse_action_type_maps_all_four_codes(raw, expected):
    assert parse_action_type(raw) == expected


@pytest.mark.parametrize("raw", ["4", "", "click", "-1", "2.0"])
def test_parse_action_type_rejects_unknown_codes(raw):
    """未知的 action_type 必须报错。静默归到 click 会污染整个漏斗的分母。"""
    with pytest.raises(TmallDataError) as excinfo:
        parse_action_type(raw)
    assert excinfo.value.category == ERROR_ACTION_TYPE


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------


def test_mmdd_is_converted_to_a_2014_date():
    assert parse_user_log_date("0511") == date(2014, 5, 11)
    assert parse_user_log_date("1112") == date(2014, 11, 12)
    assert parse_user_log_date("0101") == date(2014, 1, 1)


@pytest.mark.parametrize("raw", ["0230", "1301", "0000", "1232", "230", "511", "abcd", "", "  "])
def test_invalid_timestamps_are_rejected(raw):
    """0230 与 1301 这种「格式对、日期不存在」的值必须报错。

    如果用字符串切片拼日期再交给数据库，PostgreSQL 会拒；但那样报错发生在
    COPY 阶段，行号和列名都丢了，排查要重跑一遍 55 倍的数据。
    """
    with pytest.raises(TmallDataError) as excinfo:
        parse_user_log_date(raw)
    assert excinfo.value.category == ERROR_DATE


def test_date_range_covers_may_to_november():
    assert parse_user_log_date("0511") < parse_user_log_date("1112")


# --------------------------------------------------------------------------
# 可空与必填整数
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_blank_becomes_none_not_zero(raw):
    """空 brand_id 归一为 None。

    如果归一成 0，「无品牌」会和「品牌 ID 恰好为 0」合并成一个分组，
    之后所有按品牌聚合的数字都是错的，而且错得看不出来。
    """
    assert parse_optional_int(raw, field="brand_id") is None


def test_zero_is_preserved_as_zero():
    assert parse_optional_int("0", field="brand_id") == 0


@pytest.mark.parametrize("raw", ["abc", "1.5", "-2"])
def test_non_integer_values_are_rejected(raw):
    with pytest.raises(TmallDataError) as excinfo:
        parse_optional_int(raw, field="brand_id")
    assert excinfo.value.category == ERROR_FIELD_TYPE


def test_required_int_rejects_blank():
    with pytest.raises(TmallDataError) as excinfo:
        parse_required_int("", field="user_id")
    assert excinfo.value.category == ERROR_FIELD_TYPE


# --------------------------------------------------------------------------
# 行解码
# --------------------------------------------------------------------------


def test_parse_user_info_row_keeps_optional_fields_empty():
    assert parse_user_info_row(["123", "", "1"]).age_range is None
    assert parse_user_info_row(["123", "3", "0"]).gender == 0


def test_parse_user_log_row_renames_seller_and_category():
    """seller_id → merchant_id、cat_id → category_id 是全局约定。

    两个名字在同一份代码里混用，会让「按商家聚合」这类需求在 Silver 层
    就找不到正确的列。
    """
    row = parse_user_log_row(
        ["42", "777", "888", "999", "5", "0511", "2"], source_row_number=17
    )
    assert row.merchant_id == 999
    assert row.category_id == 888
    assert row.item_id == 777
    assert row.brand_id == 5
    assert row.event_date == date(2014, 5, 11)
    assert row.action_type == "buy"
    assert row.source_row_number == 17


def test_parse_user_log_row_treats_blank_brand_as_null():
    row = parse_user_log_row(
        ["42", "777", "888", "999", "", "0511", "0"], source_row_number=1
    )
    assert row.brand_id is None


def test_parse_user_log_row_rejects_short_rows():
    with pytest.raises(TmallDataError) as excinfo:
        parse_user_log_row(["42", "777"], source_row_number=3)
    assert excinfo.value.category == ERROR_ROW_SHAPE


def test_parse_repurchase_train_row_reads_label_not_probability():
    row = parse_repurchase_row(["9", "11", "1"], dataset_split="train")
    assert row.label == 1
    assert row.probability is None


def test_parse_repurchase_test_row_reads_probability_not_label():
    row = parse_repurchase_row(["9", "11", "0.73"], dataset_split="test")
    # 直接用 Decimal 比较：prob 走的是 Numeric 列，任何 float 中转都会引入
    # 一个「差不多但不相等」的数，而这种偏差在小数位上看起来无害，聚合后就明显了。
    assert row.probability == Decimal("0.73")
    assert row.label is None


def test_probability_is_parsed_exactly_without_float_rounding():
    """0.0021015 在二进制浮点里没有精确表示，用 float 中转就会失真。"""
    row = parse_repurchase_row(["9", "11", "0.0021015"], dataset_split="test")
    assert str(row.probability) == "0.0021015"
    assert isinstance(row.probability, Decimal)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_probability_rejects_non_finite_values(raw):
    """NaN / Infinity 能通过 Decimal 构造，但既不是概率也写不进 Numeric。

    不在这里拦住的话，错误会以「数据库类型错误」的形式出现在几十万行
    之后的 COPY 里，那时已经没有任何行号信息了。
    """
    with pytest.raises(TmallDataError) as excinfo:
        parse_repurchase_row(["9", "11", raw], dataset_split="test")
    assert excinfo.value.category == ERROR_FIELD_TYPE


@pytest.mark.parametrize("raw", ["2", "-1", "yes"])
def test_train_label_must_be_binary(raw):
    with pytest.raises(TmallDataError) as excinfo:
        parse_repurchase_row(["9", "11", raw], dataset_split="train")
    assert excinfo.value.category == ERROR_FIELD_TYPE


@pytest.mark.parametrize("raw", ["1.4", "-0.2"])
def test_test_probability_must_be_a_unit_interval(raw):
    with pytest.raises(TmallDataError) as excinfo:
        parse_repurchase_row(["9", "11", raw], dataset_split="test")
    assert excinfo.value.category == ERROR_FIELD_TYPE


def test_blank_test_probability_is_allowed():
    """官方发布的 test_format1.csv 里 prob 列**整列为空**——它留给参赛者
    写预测结果，原始数据里没有值。

    把空值当成错误的话，整份数据一行都导不进来，而且报错会出现在
    抽样之后的行上，看起来像「某些行坏了」，实际是全部。
    """
    row = parse_repurchase_row(["9", "11", ""], dataset_split="test")
    assert row.probability is None
    assert row.label is None
    assert row.dataset_split == "test"


def test_unknown_split_is_a_programming_error():
    with pytest.raises(ValueError):
        parse_repurchase_row(["9", "11", "1"], dataset_split="valid")


# --------------------------------------------------------------------------
# 流式解码 + 抽样
# --------------------------------------------------------------------------


def _log_row(user_id, action="0", brand="5"):
    return [str(user_id), "777", "888", "999", brand, "0511", action]


def test_iter_decoded_log_rows_filters_by_sampling():
    rows = [_log_row(1), _log_row(55), _log_row(110), _log_row(7)]
    decoded = list(iter_decoded_log_rows(rows, sample_modulus=55, sample_residue=0))
    assert [row.user_id for row in decoded] == [55, 110]


def test_source_row_number_counts_original_rows_not_sampled_ones():
    """source_row_number 必须指向原始文件里的位置。

    它是 event_id 稳定性的来源：如果按「抽样后的序号」编号，
    换一组抽样参数后同一个原始行的编号会变，重复导入就认不出是同一行。
    """
    rows = [_log_row(1), _log_row(55), _log_row(2), _log_row(110)]
    decoded = list(iter_decoded_log_rows(rows, sample_modulus=55, sample_residue=0))
    assert [(row.user_id, row.source_row_number) for row in decoded] == [(55, 2), (110, 4)]


def test_iter_decoded_log_rows_rejects_wrong_column_count():
    with pytest.raises(TmallDataError) as excinfo:
        list(iter_decoded_log_rows([["1", "2"]], sample_modulus=55, sample_residue=0))
    assert excinfo.value.category == ERROR_ROW_SHAPE


# --------------------------------------------------------------------------
# 错误分类与信息卫生
# --------------------------------------------------------------------------


def test_every_error_category_has_a_fixed_message():
    for category in ERROR_CATEGORIES:
        assert category in ERROR_MESSAGES
        assert ERROR_MESSAGES[category].strip()


def test_error_messages_never_leak_connection_details():
    """错误分类是受控枚举，文案是我们自己写的，不含任何调用方输入。

    这是「错误信息不能包含数据库密码等敏感信息」的第一道保障：
    能写进 tmall_ingestion_runs.error_message 的只有这些固定文案。
    """
    forbidden = ("password", "postgresql://", "postgres://", "asyncpg", "api_key")
    for message in ERROR_MESSAGES.values():
        lowered = message.lower()
        for token in forbidden:
            assert token not in lowered, f"错误文案里出现了敏感词：{token}"


def test_error_detail_does_not_echo_the_whole_row():
    """报错只带「第几行、哪一列」，不回显整行内容。"""
    error = TmallDataError(ERROR_FIELD_TYPE, "brand_id 不是整数：'abc'")
    assert "user_id" not in str(error)
