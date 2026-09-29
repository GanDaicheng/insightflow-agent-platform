"""天猫导入流水线的测试：ZIP 流式读取、解码、抽样、导入决策、错误分类。

这些测试**都不连数据库**。真实入库路径由 scripts/verify_tmall_data.py
对着真实数据校验——把 100 万行的 COPY 塞进单元测试只会让它变慢变脆，
而它验证的东西（COPY 是否写对了行）恰好是 verify 脚本更擅长验证的。
"""

import zipfile

import pytest

from app.services.tmall_data import (
    ERROR_ACTION_TYPE,
    ERROR_CONSTRAINT,
    ERROR_DATABASE,
    ERROR_DATE,
    ERROR_DUPLICATE_KEY,
    ERROR_HEADER,
    ERROR_ROW_SHAPE,
    TmallDataError,
)
from app.services.tmall_pipeline import (
    EVENTS_COLUMNS,
    REPURCHASE_COLUMNS,
    USERS_COLUMNS,
    IngestDecision,
    SamplingParams,
    StreamStats,
    TmallIngestError,
    build_event_records,
    build_user_records,
    copy_records,
    decide_ingest,
    existing_data_summary,
    open_member,
    report_every,
    scan_archive,
)
from tests.tmall_fixtures import build_zip, zip_with_bom

from datetime import date
from decimal import Decimal

PARAMS = SamplingParams(modulus=5, residue=0)


def _open(path):
    return zipfile.ZipFile(path)


# --------------------------------------------------------------------------
# 抽样参数
# --------------------------------------------------------------------------


def test_default_sampling_params_match_the_registered_baseline():
    assert (SamplingParams().modulus, SamplingParams().residue) == (55, 0)


@pytest.mark.parametrize(
    ("modulus", "residue"),
    [(0, 0), (-1, 0), (5, 5), (5, -1), (5, 6)],
)
def test_invalid_sampling_params_fail_at_construction(modulus, residue):
    """非法参数必须在构造 SamplingParams 的那一刻就炸。

    让它一路走到底的后果是：取模条件静默失效，导入「成功」但数据是错的。
    """
    with pytest.raises(ValueError):
        SamplingParams(modulus=modulus, residue=residue)


def test_sampling_params_reject_booleans():
    """bool 是 int 的子类，不显式拒绝的话 True 会变成一个合法参数。"""
    with pytest.raises(ValueError):
        SamplingParams(modulus=True, residue=0)


def test_sampling_params_describe_is_human_readable():
    assert SamplingParams(modulus=55, residue=0).describe() == "user_id % 55 == 0"


# --------------------------------------------------------------------------
# 导入决策（纯函数）
# --------------------------------------------------------------------------


EMPTY = {"tmall_users": 0, "tmall_user_events": 0, "tmall_repurchase_samples": 0}
FILLED = {"tmall_users": 7712, "tmall_user_events": 998542, "tmall_repurchase_samples": 9558}


def test_empty_tables_import():
    decision = decide_ingest(EMPTY, None, replace=False)
    assert decision.action == "import"
    assert decision.should_import


def test_same_file_already_succeeded_is_skipped():
    decision = decide_ingest(FILLED, 42, replace=False)
    assert decision.action == "skip"
    assert "42" in decision.reason


def test_replace_forces_a_real_import_even_when_already_succeeded():
    """--replace 是操作者显式的指令，不能变成一句「已跳过，什么也没做」。"""
    decision = decide_ingest(FILLED, 42, replace=True)
    assert decision.action == "import"


def test_existing_data_without_replace_is_refused():
    decision = decide_ingest(FILLED, None, replace=False)
    assert decision.action == "refuse"
    assert "--replace" in decision.reason


def test_replace_allows_overwriting_other_data():
    assert decide_ingest(FILLED, None, replace=True).action == "import"


def test_ledger_says_done_but_tables_are_empty_still_imports():
    """台账说「导过了」但表是空的（有人手工清过表、或库从备份恢复）。

    此时跳过会留下一个空库，而且看起来完全成功——这是最坏的一种结果。
    所以判定「跳过」必须同时看台账**和**表里的实际数据。
    """
    decision = decide_ingest(EMPTY, 42, replace=False)
    assert decision.action == "import"


def test_partially_filled_tables_count_as_existing_data():
    partial = {"tmall_users": 0, "tmall_user_events": 1000, "tmall_repurchase_samples": 0}
    assert decide_ingest(partial, None, replace=False).action == "refuse"


def test_existing_data_summary_lists_only_non_empty_tables():
    assert existing_data_summary(EMPTY) is None
    assert "tmall_users" in (existing_data_summary(FILLED) or "")
    assert "tmall_repurchase_samples" not in (
        existing_data_summary({"tmall_users": 1, "tmall_user_events": 0,
                               "tmall_repurchase_samples": 0}) or ""
    )


def test_decision_is_a_frozen_value_object():
    decision = IngestDecision("import", "理由")
    with pytest.raises(Exception):
        decision.action = "skip"  # type: ignore[misc]


# --------------------------------------------------------------------------
# ZIP 打开与表头校验
# --------------------------------------------------------------------------


def test_open_member_reads_data_rows_without_the_header(tmp_path):
    path = build_zip(tmp_path / "d.zip")
    with _open(path) as archive:
        reader = open_member(archive, "user_info")
        try:
            rows = list(reader.rows)
        finally:
            reader.close()
    assert rows[0] == ["1", "", "0"]
    assert len(rows) == 5


def test_open_member_accepts_a_utf8_bom(tmp_path):
    """Windows 导出的 CSV 常带 BOM。用 utf-8 读会让第一列变成 '\\ufeffuser_id'，
    报错方向完全指错。"""
    path = zip_with_bom(tmp_path / "bom.zip")
    with _open(path) as archive:
        reader = open_member(archive, "user_info")
        try:
            assert list(reader.rows) == [["5", "2", "0"]]
        finally:
            reader.close()


def test_open_member_rejects_a_reordered_header_immediately(tmp_path):
    """表头校验必须发生在**打开时**，而不是消费第一行时。

    否则前三个文件已经 COPY 完了，才发现第四个文件的表头不对——
    事务虽然能回滚，但几分钟已经白花，而且报错会让人以为「导到一半坏了」。
    """
    bad_header = "user_id,cat_id,item_id,seller_id,brand_id,time_stamp,action_type"
    path = build_zip(tmp_path / "d.zip", user_log_header=bad_header)
    with _open(path) as archive:
        with pytest.raises(TmallDataError) as excinfo:
            open_member(archive, "user_log")
    assert excinfo.value.category == ERROR_HEADER


def test_open_member_rejects_a_missing_member(tmp_path):
    path = build_zip(tmp_path / "d.zip", omit=("data_format1/test_format1.csv",))
    with _open(path) as archive:
        with pytest.raises(TmallDataError) as excinfo:
            open_member(archive, "test")
    assert excinfo.value.category == ERROR_HEADER


def test_open_member_rejects_an_empty_file(tmp_path):
    import io

    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("data_format1/user_info_format1.csv", "")
    with _open(path) as archive:
        with pytest.raises(TmallDataError) as excinfo:
            open_member(archive, "user_info")
    assert excinfo.value.category == ERROR_HEADER


def test_open_member_closes_the_stream_when_the_header_is_bad(tmp_path):
    """表头不对时必须把 ZipExtFile 关掉，不能把半开的句柄留给调用方。"""
    path = build_zip(tmp_path / "d.zip", user_info_header="a,b,c")
    with _open(path) as archive:
        with pytest.raises(TmallDataError):
            open_member(archive, "user_info")
        # 句柄已关，重复关闭不应抛异常，也不应影响后续读取
        reader = open_member(archive, "user_log")
        reader.close()


# --------------------------------------------------------------------------
# 记录流：抽样 + 改名 + 编码解码
# --------------------------------------------------------------------------


def _collect(path, key: str, *, params: SamplingParams = PARAMS):
    with _open(path) as archive:
        scan = scan_archive(archive, params, progress=None)
        try:
            return list(scan.streams[key]), scan.stats
        finally:
            scan.close()


def test_scan_archive_validates_every_header_before_returning(tmp_path):
    """任意一个文件表头不对，scan_archive 就必须整体失败，不返回半成品。"""
    path = build_zip(tmp_path / "d.zip", train_header="user_id,merchant_id,wrong")
    with _open(path) as archive:
        with pytest.raises(TmallDataError) as excinfo:
            scan_archive(archive, PARAMS, progress=None)
    assert excinfo.value.category == ERROR_HEADER


def test_user_records_are_filtered_by_sampling(tmp_path):
    path = build_zip(tmp_path / "d.zip")
    records, stats = _collect(path, "user_info")
    assert [record[0] for record in records] == [5, 10]
    assert stats["user_info"].raw_rows == 5
    assert stats["user_info"].sampled_rows == 2


def test_user_records_keep_optional_fields_as_none(tmp_path):
    path = build_zip(tmp_path / "d.zip")
    records, _ = _collect(path, "user_info")
    assert records == [(5, 2, 0), (10, None, 1)]


def test_event_records_rename_seller_and_category_and_decode_fields(tmp_path):
    path = build_zip(tmp_path / "d.zip")
    records, stats = _collect(path, "user_log")

    assert stats["user_log"].raw_rows == 7
    # 7 行里 user 1 的那行被丢弃，剩 6 行：user 5 四行 + user 10 两行
    assert stats["user_log"].sampled_rows == 6

    first = records[0]
    assert first == (2, 5, 111, 11, 101, None, date(2014, 5, 11), "buy")
    # 元组顺序必须与 EVENTS_COLUMNS 完全一致，否则 COPY 会把列写串
    assert len(first) == len(EVENTS_COLUMNS)


def test_event_source_row_number_points_at_the_original_file_position(tmp_path):
    """source_row_number 是原始文件里的行号，不是抽样后的序号。

    它是 event_id 稳定性的来源：换一组抽样参数后，同一个原始行的编号
    必须还是同一个，否则重复导入认不出是同一行。
    """
    path = build_zip(tmp_path / "d.zip")
    records, _ = _collect(path, "user_log")
    # 第 1 行是 user 1（被丢弃），其余六行都是被抽中的用户
    assert [record[0] for record in records] == [2, 3, 4, 5, 6, 7]


def test_event_records_cover_all_four_action_types(tmp_path):
    path = build_zip(tmp_path / "d.zip")
    records, _ = _collect(path, "user_log")
    assert {record[-1] for record in records} == {"click", "cart", "favorite", "buy"}


def test_event_records_decode_the_last_day_of_the_dataset(tmp_path):
    path = build_zip(tmp_path / "d.zip")
    records, _ = _collect(path, "user_log")
    assert max(record[6] for record in records) == date(2014, 11, 12)


def test_train_records_carry_label_and_no_probability(tmp_path):
    path = build_zip(tmp_path / "d.zip")
    records, stats = _collect(path, "train")
    assert records == [(5, 201, "train", 1, None), (10, 202, "train", 0, None)]
    assert stats["train"].raw_rows == 3
    assert stats["train"].sampled_rows == 2
    assert len(records[0]) == len(REPURCHASE_COLUMNS)


def test_test_records_carry_probability_and_no_label(tmp_path):
    """test 行的 prob 允许为空——真实数据的 prob 列整列为空。"""
    path = build_zip(tmp_path / "d.zip")
    records, _ = _collect(path, "test")
    assert records == [
        (5, 301, "test", None, None),
        (10, 302, "test", None, Decimal("0.1250000")),
    ]


def test_probability_is_decoded_as_decimal_not_float(tmp_path):
    """prob 的目标列是 Numeric(8,7)，float 表示不了 0.125 这类值。"""
    path = build_zip(tmp_path / "d.zip")
    records, _ = _collect(path, "test")
    assert isinstance(records[1][4], Decimal)


def test_a_different_residue_selects_a_different_user_set(tmp_path):
    path = build_zip(tmp_path / "d.zip")
    records, _ = _collect(path, "user_info", params=SamplingParams(modulus=5, residue=1))
    assert [record[0] for record in records] == [1]


def test_record_builders_reject_rows_with_the_wrong_width(tmp_path):
    path = build_zip(
        tmp_path / "d.zip",
        user_log=("5,111,11,101,5,0511,0,extra",),
    )
    with pytest.raises(TmallDataError) as excinfo:
        _collect(path, "user_log")
    assert excinfo.value.category == ERROR_ROW_SHAPE


def test_record_builders_reject_too_few_columns(tmp_path):
    path = build_zip(tmp_path / "d.zip", user_info=("5,2",))
    with pytest.raises(TmallDataError) as excinfo:
        _collect(path, "user_info")
    assert excinfo.value.category == ERROR_ROW_SHAPE


def test_record_builders_reject_an_invalid_action_type(tmp_path):
    path = build_zip(tmp_path / "d.zip", user_log=("5,111,11,101,5,0511,9",))
    with pytest.raises(TmallDataError) as excinfo:
        _collect(path, "user_log")
    assert excinfo.value.category == ERROR_ACTION_TYPE


def test_record_builders_reject_an_impossible_date(tmp_path):
    path = build_zip(tmp_path / "d.zip", user_log=("5,111,11,101,5,0230,0",))
    with pytest.raises(TmallDataError) as excinfo:
        _collect(path, "user_log")
    assert excinfo.value.category == ERROR_DATE


def test_non_integer_user_id_in_the_log_is_reported_as_a_row_problem(tmp_path):
    """user_id 解析失败要归类成行格式问题，而不是让它冒成 ValueError。

    ValueError 会一路冒到 CLI，被兜底的 except 归到 database_error——
    一个纯粹的数据问题被报告成数据库问题，排查方向直接跑偏。
    """
    path = build_zip(tmp_path / "d.zip", user_log=("abc,111,11,101,5,0511,0",))
    with pytest.raises(TmallDataError) as excinfo:
        _collect(path, "user_log")
    assert excinfo.value.category == ERROR_ROW_SHAPE


def test_builders_can_be_driven_directly_without_a_zip():
    """三个 builder 都是「输入行迭代器、输出元组」的纯函数，
    不依赖 zipfile——这让边界用例不需要造一个真实 ZIP 就能覆盖。"""
    stats = StreamStats()
    rows = iter([["5", "111", "11", "101", "", "0511", "2"]])
    records = list(build_event_records(rows, PARAMS, stats))
    assert records == [(1, 5, 111, 11, 101, None, date(2014, 5, 11), "buy")]
    assert stats.raw_rows == 1 and stats.sampled_rows == 1


def test_user_builder_and_repurchase_builder_are_streams(tmp_path):
    """记录流必须是惰性的：一次性 materialize 会把 100 万行堆进内存。"""
    stats = StreamStats()
    consumed: list[int] = []

    def rows():
        for index in range(1, 6):
            consumed.append(index)
            # 全部用会被抽中的 user_id，这样「取一条」与「读一行」才是同一个动作，
            # 否则 next() 为了找到一条被抽中的行本来就可能读完整份数据。
            yield ["5", "1", "0"]

    stream = build_user_records(rows(), PARAMS, stats)
    assert consumed == []  # 还没消费
    next(stream)
    assert consumed == [1]  # 只读了一行


def test_us_and_event_column_lists_are_in_record_order():
    """列清单与 builder 产出的元组顺序必须一一对应。

    写反了不会报错——COPY 会忠实地把 item_id 写进 category_id，
    类型还都是 bigint，直到有人发现「类目数只有几十个」才察觉。
    """
    assert USERS_COLUMNS == ("user_id", "age_range", "gender")
    assert EVENTS_COLUMNS == (
        "source_row_number", "user_id", "item_id", "category_id",
        "merchant_id", "brand_id", "event_date", "action_type",
    )
    # event_id 由 BIGSERIAL 生成，绝不能出现在 COPY 列清单里
    assert "event_id" not in EVENTS_COLUMNS
    assert REPURCHASE_COLUMNS == (
        "user_id", "merchant_id", "dataset_split", "label", "probability",
    )


# --------------------------------------------------------------------------
# 进度上报
# --------------------------------------------------------------------------


def test_report_every_emits_progress_at_the_interval():
    messages: list[str] = []
    records = [(index,) for index in range(1, 11)]
    result = list(
        report_every(iter(records), every=4, label="events", progress=messages.append)
    )
    assert result == records
    assert len(messages) == 2
    assert "4" in messages[0] and "8" in messages[1]


def test_report_every_is_a_noop_without_a_callback():
    records = [(1,), (2,)]
    assert list(report_every(iter(records), every=1, label="x", progress=None)) == records


# --------------------------------------------------------------------------
# 错误分类
# --------------------------------------------------------------------------


def test_ingest_error_message_is_controlled_and_has_no_credentials():
    error = TmallIngestError(ERROR_HEADER)
    assert "密码" not in str(error)
    assert "postgresql" not in str(error)


def test_missing_zip_file_fails_with_a_controlled_error(tmp_path):
    import asyncio

    from app.services.tmall_pipeline import ingest_archive

    with pytest.raises(TmallIngestError) as excinfo:
        asyncio.run(
            ingest_archive(
                tmp_path / "nope.zip", PARAMS, engine=None, dry_run=True
            )
        )
    assert excinfo.value.category == ERROR_HEADER


def test_non_dry_run_requires_an_engine(tmp_path):
    import asyncio

    from app.services.tmall_pipeline import ingest_archive

    path = build_zip(tmp_path / "d.zip")
    with pytest.raises(ValueError):
        asyncio.run(ingest_archive(path, PARAMS, engine=None, dry_run=False))


def test_copy_records_rejects_a_non_positive_batch_interval(tmp_path):
    import asyncio

    from app.services.tmall_pipeline import ingest_archive

    path = build_zip(tmp_path / "d.zip")
    with pytest.raises(ValueError):
        asyncio.run(
            ingest_archive(path, PARAMS, engine=None, dry_run=True, batch_size=0)
        )


class _FakeUniqueViolation(Exception):
    """模拟 asyncpg 的唯一约束冲突。"""


class _FakeDbError(Exception):
    """模拟带 SQLSTATE 的数据库异常。"""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("database error")
        self.sqlstate = sqlstate


def _wrapped(sqlstate: str) -> Exception:
    """构造一个「SQLAlchemy 外壳 + 带 sqlstate 的 orig」的异常。

    真实形状就是这样：SQLAlchemy 把 asyncpg 的具体异常统一包成
    IntegrityError，原始错误码只留在 `.orig` 上。
    """
    error = _FakeDbError(sqlstate)
    wrapper = Exception("(builtins) wrapped")
    wrapper.orig = error  # type: ignore[attr-defined]
    return wrapper


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("23505", ERROR_DUPLICATE_KEY),   # unique_violation
        ("23514", ERROR_CONSTRAINT),      # check_violation
        ("23503", ERROR_CONSTRAINT),      # foreign_key_violation
        ("23502", ERROR_CONSTRAINT),      # not_null_violation
        ("42P01", ERROR_DATABASE),        # undefined_table：表没建，属于部署问题
        ("25P02", ERROR_DATABASE),        # 事务已中止
    ],
)
def test_sqlstate_drives_the_error_category(sqlstate, expected):
    """用 SQLSTATE 而不是异常类名分类。

    SQLAlchemy 会把 asyncpg 的具体异常统一包成 IntegrityError，
    类名分不出「主键重复」和「CHECK 约束不满足」——而这两种错的
    排查方向完全不同（前者多半是重复导入，后者是解码逻辑写错了）。
    """
    from app.services.tmall_pipeline import _category_of

    assert _category_of(_wrapped(sqlstate)) == expected


def test_unique_violation_maps_to_the_duplicate_category():
    from app.services.tmall_pipeline import _category_of

    assert _category_of(_FakeUniqueViolation()) == ERROR_DATABASE


def test_category_of_never_inspects_the_exception_text():
    """分类只看异常类型与 SQLSTATE。

    数据库异常的原文里可能带着连接串，而这个分类结果会被写进
    tmall_ingestion_runs.error_message —— 一张将来要查询、备份、导出的表。
    """

    class WeirdError(Exception):
        pass

    error = WeirdError("postgresql://user:secret@host/db")
    error.orig = _FakeDbError("08006")  # type: ignore[attr-defined]
    from app.services.tmall_pipeline import _category_of

    assert _category_of(error) == ERROR_DATABASE


def test_data_errors_keep_their_own_category():
    from app.services.tmall_pipeline import _category_of

    assert _category_of(TmallDataError(ERROR_ACTION_TYPE, "x")) == ERROR_ACTION_TYPE
    assert _category_of(TmallIngestError(ERROR_HEADER)) == ERROR_HEADER


# --------------------------------------------------------------------------
# COPY 的签名约束（不连数据库）
# --------------------------------------------------------------------------


def test_copy_records_is_an_async_function_over_a_lazy_stream():
    import inspect

    assert inspect.iscoroutinefunction(copy_records)
    # 第二个参数是列清单：没有它，元组的列顺序就无从校验
    parameters = list(inspect.signature(copy_records).parameters)
    assert parameters[:3] == ["conn", "table", "columns"]
    assert parameters[3] == "records"
