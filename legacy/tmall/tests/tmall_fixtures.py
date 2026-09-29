"""天猫数据测试用的 ZIP 构造工具。

真实的 data_format1.zip 有 377MB，不可能进单元测试。这里用同样的
目录结构与表头，数据量缩到十几行——被测的是**格式契约**，不是数据规模。
规模由 scripts/verify_tmall_data.py 对着真实数据校验。
"""

import io
import zipfile
from pathlib import Path

USER_INFO_HEADER = "user_id,age_range,gender"
USER_LOG_HEADER = "user_id,item_id,cat_id,seller_id,brand_id,time_stamp,action_type"
TRAIN_HEADER = "user_id,merchant_id,label"
TEST_HEADER = "user_id,merchant_id,prob"

# 抽样参数 5/0 时被选中的用户是 5、10、15…（user_id % 5 == 0）。
# 这份样例里因此既有会被抽中的用户，也有会被丢弃的，两类都能覆盖到。
SAMPLED_USER_IDS = (5, 10)
DROPPED_USER_IDS = (1, 2, 3)

DEFAULT_USER_INFO_ROWS = (
    "1,,0",
    "2,3,1",
    "5,2,0",
    "10,,1",
    "12,8,2",
)

# 每个被抽中的用户都出现了多个动作，方便验证 action_type 的四种取值。
DEFAULT_LOG_ROWS = (
    # user 1 不在抽样范围里，这一行必须被丢掉
    "1,111,11,101,5,0511,0",
    # brand_id 为空 → NULL；action_type=2 → buy
    "5,111,11,101,,0511,2",
    # action_type=1 → cart
    "5,222,22,102,7,1112,1",
    # action_type=3 → favorite
    "5,222,22,102,7,1112,3",
    # 同一天同商品再来一条 click
    "5,111,11,101,5,1112,0",
    # 另一个被抽中的用户
    "10,333,33,103,9,0601,0",
    # 跨年边界：1112 是数据里的最后一天
    "10,333,33,103,9,1112,2",
)

DEFAULT_TRAIN_ROWS = (
    "1,201,1",   # user 1 不在抽样范围
    "5,201,1",
    "10,202,0",
)

DEFAULT_TEST_ROWS = (
    "2,301,0.25",  # user 2 不在抽样范围
    # 第二列之后为空照抄真实数据：官方 test_format1.csv 的 prob 列整列为空
    "5,301,",
    "10,302,0.1250000",
)


def build_zip(
    path: Path,
    *,
    user_info: tuple[str, ...] = DEFAULT_USER_INFO_ROWS,
    user_log: tuple[str, ...] = DEFAULT_LOG_ROWS,
    train: tuple[str, ...] = DEFAULT_TRAIN_ROWS,
    test: tuple[str, ...] = DEFAULT_TEST_ROWS,
    user_info_header: str = USER_INFO_HEADER,
    user_log_header: str = USER_LOG_HEADER,
    train_header: str = TRAIN_HEADER,
    test_header: str = TEST_HEADER,
    omit: tuple[str, ...] = (),
) -> Path:
    """在 path 位置写一个 data_format1.zip。

    所有参数都有默认值，是为了让「只想改一个文件」的测试不必把
    另外三个也抄一遍——抄写是测试之间产生意外耦合的主要来源。
    """
    members = {
        "data_format1/user_info_format1.csv": (user_info_header, user_info),
        "data_format1/user_log_format1.csv": (user_log_header, user_log),
        "data_format1/train_format1.csv": (train_header, train),
        "data_format1/test_format1.csv": (test_header, test),
    }

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("data_format1/", "")
        for name, (header, rows) in members.items():
            if name in omit:
                continue
            body = "\n".join([header, *rows]) + "\n"
            archive.writestr(name, body)

    return path


def zip_with_bom(path: Path, *, header: str = USER_INFO_HEADER) -> Path:
    """写一个带 UTF-8 BOM 的 ZIP。

    Windows 上导出的 CSV 很容易带上 BOM，这是「表头看起来完全一样、
    校验却失败」最常见的原因，必须有一条测试钉住它。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "data_format1/user_info_format1.csv",
            "﻿" + header + "\n5,2,0\n",
        )
        archive.writestr(
            "data_format1/user_log_format1.csv", USER_LOG_HEADER + "\n5,111,11,101,5,0511,0\n"
        )
        archive.writestr("data_format1/train_format1.csv", TRAIN_HEADER + "\n5,201,1\n")
        archive.writestr("data_format1/test_format1.csv", TEST_HEADER + "\n5,301,0.5\n")
    path.write_bytes(buffer.getvalue())
    return path
