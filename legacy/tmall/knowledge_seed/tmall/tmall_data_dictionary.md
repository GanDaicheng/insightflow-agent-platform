# 天猫数据集数据字典

> 文档类型：数据字典
> 适用范围：天猫 IJCAI 2015 数据集的 Silver 与 Gold 两层表
> 使用对象：数据分析、智能问数 Agent、取数开发

## 1. 两层结构

数据分成两层，它们的分工是**权限**，不是性能：

| 层 | 表 | 谁能查 |
| --- | --- | --- |
| Silver（明细） | `tmall_users`、`tmall_user_events`、`tmall_repurchase_samples` | 只有导入与校验脚本 |
| Gold（汇总） | `tmall_daily_metrics` 等六张 | 智能问数只能查这一层 |

明细层保存与原始 CSV 一一对应的记录，是「这份数据到底说了什么」的唯一事实来源。
汇总层保存预先算好的结果。

**智能问数只允许访问 Gold 层。** 明细表不在查询白名单里，
所以「大模型能不能拉出某一个用户的完整行为轨迹」这个问题，
在表这一层就有答案：不能。

## 2. 用户表 `tmall_users`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `user_id` | 整数 | 用户 ID，主键。已抽样，不是全量用户 |
| `age_range` | 整数，可空 | 年龄段**编码**，不是具体年龄 |
| `gender` | 整数，可空 | 性别**编码**，不是文字 |

用户表只有画像两列，**没有任何直接标识**：没有姓名、手机号、地址、邮箱。
这不是「暂时没导入」，而是这份数据集本身就不包含。

`age_range` / `gender` 的编码含义由数据集说明给出，表格里存的是编码原值。
分析时应当按编码分组统计人数，**不要**把它翻译成「20 岁」「女性」这类具体表述——
编码与真实属性的映射表不在数据里，转述容易出现偏差。

## 3. 行为日志表 `tmall_user_events`

一行 = 一次行为记录。**一行不等于一笔订单。**

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `event_id` | 整数 | 代理主键，数据库生成 |
| `source_row_number` | 整数 | 在原始文件里的数据行序号（表头之后从 1 开始），唯一 |
| `user_id` | 整数 | 用户 ID |
| `item_id` | 整数 | 商品 ID |
| `category_id` | 整数 | 类目 ID，**来自原始字段 `cat_id`** |
| `merchant_id` | 整数 | 商家 ID，**来自原始字段 `seller_id`** |
| `brand_id` | 整数，可空 | 品牌 ID |
| `event_date` | 日期 | 行为发生的日期 |
| `action_type` | 文本 | `click` / `cart` / `favorite` / `buy` |

### 需要特别注意的三处

**命名统一。** 原始列名是 `cat_id` 和 `seller_id`，入库时统一改成
`category_id` 和 `merchant_id`。所有分析口径都使用改名后的字段，
不要在 SQL 里再写 `cat_id` / `seller_id`。

**空值。** 原始数据用空字符串表示「没有品牌」。入库时归一为 `NULL`，
**不是 0**。如果把空值当成 0 存，「无品牌」就会和「品牌 ID 恰好为 0」
合并成同一个分组，之后所有按品牌聚合的数字都是错的。

**日期。** 原始 `time_stamp` 只有四位 `MMDD`（例如 `0511`），
**不含年份**。年份 2014 由数据集说明确定，不是从数据里推断出来的。
所以日期范围是 2014-05-11 ~ 2014-11-12。

### 为什么明细层有唯一约束

`source_row_number` 有唯一约束。它的作用是让「同一份文件导入两次」
在数据库层就冲突，而不是依赖调用方自觉。有了它，
重复导入不可能悄悄产生一份重复数据。

## 4. 复购样本表 `tmall_repurchase_samples`

`train` 与 `test` 两个文件合并在这张表里，靠 `dataset_split` 区分。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `dataset_split` | 文本 | `train` 或 `test` |
| `user_id` | 整数 | 用户 ID |
| `merchant_id` | 整数 | 商家 ID |
| `label` | 整数，可空 | 是否复购：1 是、0 否。**只有 train 有值** |
| `probability` | 数值，可空 | 预测概率。**本地这一版整列为空** |

`user_id + merchant_id + dataset_split` 唯一。

`label` 与 `dataset_split` 的归属由数据库约束保证：
train 行必须有 `label`、不能有 `probability`；test 行不能有 `label`。

### `test` 集为什么没有概率

官方发布的 `test_format1.csv` 里 `prob` 列是**整列为空**的——
那一列留给参赛者写预测结果，原始数据里没有值。
所以 `tmall_repurchase_metrics.average_probability` 通常为 `NULL`。

`NULL` 在这里是**正确**的结果，表示「没有预测概率」。
不要把它读成 0——「预测概率为 0」和「没有预测概率」是两件完全不同的事。

## 5. 六张 Gold 汇总表

| 表 | 粒度 | 主键 |
| --- | --- | --- |
| `tmall_daily_metrics` | 日期 × 行为 | `metric_date`, `action_type` |
| `tmall_merchant_metrics` | 商家 | `merchant_id` |
| `tmall_category_metrics` | 类目 | `category_id` |
| `tmall_user_metrics` | 用户 | `user_id` |
| `tmall_funnel_metrics` | 行为（共四行） | `action_type` |
| `tmall_repurchase_metrics` | 数据集切分 × 标签分组 | `dataset_split`, `label_group` |

### 共有的计数字段

商家、类目、用户三张表都有同一组行为计数：

`click_count` / `cart_count` / `favorite_count` / `buy_count`

**这四个都是「行为记录数」，不是订单数。** 同一人同一天同一商品
可以有多条同类记录，数据里也没有订单号把它们归并。

### `buy_user_rate` 不是转化率

`buy_user_rate` = 该维度下有购买行为的用户数 ÷ 有任意行为的用户数。
它是一个**行为口径的比例**，回答的是「来过的用户里有多少人买过」，
**不是**电商意义上的转化率。转化率需要 session，这份数据里没有。

### 商家表的两个复购字段

| 字段 | 含义 |
| --- | --- |
| `repeat_buy_user_count` | 在该商家有过 **2 条及以上购买行为**的去重用户数 |
| `repeat_buy_user_rate` | 它 ÷ `buy_user_count` |

分母是**购买用户数**而不是全部行为用户数：复购率问的是
「在这个商家买过的人里有多少买了不止一次」，把从没买过的人
放进分母会得到一个偏低的数。

**类目表没有这两个字段。** 同一个类目下的两次购买来自两家不同的店，
那是品类偏好，不是复购。

### 用户表的两个标志不是一回事

| 字段 | 含义 | 这是什么 |
| --- | --- | --- |
| `repeat_buy_flag` | 在**同一个商家**买过 ≥2 次 | **复购** |
| `multi_merchant_buy_flag` | 在 **≥2 个不同商家**买过 | **购买广度** |
| `buy_merchant_count` | 买过的去重商家数 | 购买广度的程度 |

`multi_merchant_buy_flag` 恒等于 `buy_merchant_count >= 2`。

**这两个标志会给出相反的答案**：在 10 家店各买 1 次的人广度拉满、
复购为零；只在一家店买了 5 次的人正好相反。把广度当成复购，
所有复购相关的结论都会反过来。

### `tmall_repurchase_metrics` 的分组

`label_group` 有三个取值：

- `positive`：label = 1（train）
- `negative`：label = 0（train）
- `unlabeled`：无标签（test）

用字符串分组而不是 `label = -1` 的哨兵值，是为了让「无标签」在任何
按 label 聚合的查询里都不可能被误当成真标签。

### `positive_rate` 在 train 的两行上是同一个数

正样本占比是**整个 train 集**的属性，不是某一行的属性，
所以 `positive` 与 `negative` 两行存放的是**同一个值**。

查询时把条件写全：

```sql
WHERE dataset_split = 'train' AND label_group = 'positive'
```

只写 `dataset_split = 'train'` 会同时匹配两行。虽然现在两行同值，
但写全条件是唯一能防住「将来有人改回按组各算、negative 行变成 0」的做法——
而那个 0 看起来完全像个合法答案。test 的 `unlabeled` 行这一列是
`NULL`（不是 0）。

## 6. 导入台账 `tmall_ingestion_runs`

记录每一次导入尝试：文件名、ZIP 的 SHA256、抽样参数、状态
（`running` / `succeeded` / `failed`）、原始行数、导入行数、开始与完成时间、
错误分类。

其中 `error_category` 是受控枚举，`error_message` 只写固定的中文说明，
**不含任何数据库连接信息**。排查失败原因看这两列就够了。
