# 天猫数据集指标口径

> 文档类型：指标定义
> 适用范围：天猫 IJCAI 2015 数据集的六张 Gold 汇总表
> 使用对象：数据分析、智能问数 Agent、取数开发

## 1. 先记住三条红线

在给出任何天猫指标之前，先确认它没有踩到下面三条中的任何一条。

### 红线一：没有金额，算不出 GMV

数据里没有商品价格、优惠、运费，也没有订单金额。
因此 **GMV、销售额、收入、客单价、利润、净销售额全部算不出来**。

这不是「暂时缺少字段」，而是这份数据集从设计上就不包含交易金额。
问「天猫的 GMV 是多少」时，正确的回答是说明这一点，
**而不是给一个数字**——任何以金额形式出现的天猫答案都是编造的。

### 红线二：`buy` 是行为记录，不是订单

`action_type = 'buy'` 表示「这个用户在这个商品上发生了购买行为」。
它**不表示一笔订单**：

- 同一个人、同一天、同一件商品可以出现多条 `buy` 记录；
- 数据里没有订单号，无法把这些记录归并成订单。

所以 `buy_count` 是**购买行为记录数**，不是订单量。
把 `buy_count` 叫作「订单数」会让所有基于它的判断（客单价、每单价件数）
全部失去依据。

仍然能算的是**去重用户数**：`tmall_user_metrics.buy_count` 记的是行为条数，
而 `tmall_funnel_metrics` 里 `buy` 那一行的 `user_count` 是
「有过购买行为的去重用户数」——后者是可以放心使用的。

### 红线三：行为漏斗不是 session 顺序漏斗

数据里**没有 session_id**，也没有「用户在这一次访问里依次做了什么」。
因此算不出「点击之后加购再购买」这种路径转化率。

能算的只有**用户级去重口径**：

- 有过点击行为的去重用户数；
- 有过购买行为的去重用户数；
- 两者相除得到的比例。

这个比例回答的是「点击过的人里有多少人也买过」，**不是**转化率。
一个人可能先买了才点击，也可能在一次访问里只点击、在另一天购买。
把它讲成「转化漏斗」会让听众以为存在一条可优化的路径，而数据并不支持这个结论。

## 2. 可以放心使用的指标

### 行为记录数

```sql
SELECT SUM(event_count) FROM tmall_funnel_metrics
```

统计周期内所有行为记录的条数。四条行为之和。
**单位是「条」，不是「次访问」也不是「笔订单」。**

### 行为用户数

```sql
SELECT action_type, user_count FROM tmall_funnel_metrics
```

每种行为的**去重用户数**，整个周期上做去重。
四行分别是点击、加购、收藏、购买的用户数。

关键限制：**不能把每天的用户数相加**。同一个人在多天都有点击，
按天相加会把他数成多个人。要「按天看趋势」请用
`tmall_daily_metrics.user_count`，那是当天口径；要「整个周期有多少人」请用
`tmall_funnel_metrics.user_count`。两个口径都对，但混用一定错。

### 购买用户占比

```sql
SELECT user_rate FROM tmall_funnel_metrics WHERE action_type = 'buy'
```

分母固定是**点击用户数**，不随排序变化。所以四行的 `user_rate` 可以横向比较，
点击那一行恒为 1。

这个数字的业务读法是「点击过的人里，有多大比例也产生过购买行为」。
它适合用来描述人群的重合程度，不适合用来做「漏斗优化」的结论。

**`user_rate` 可以大于 1，这不是错误。** 它是「相对点击的倍数」而不是占比：
别的动作的用户并不一定是点击用户的子集——完全可以有人一次都没点过
就直接买了。所以它**不能当成百分比去展示**，也不要给它加上 `%`。
要表达「该行为占全部活跃用户的比例」，需要另换分母。

### 商家 / 类目维度的对比

```sql
SELECT merchant_id, buy_user_count, buy_user_rate
FROM tmall_merchant_metrics
ORDER BY buy_user_count DESC LIMIT 10
```

用于「哪些商家的购买用户最多」这类排行。
类目维度把表名换成 `tmall_category_metrics`、字段换成 `category_id` 即可。

注意 `buy_user_rate` 在这里是「该商家的购买用户占该商家行为用户的比例」，
不同商家之间可比，但它仍然不是转化率。

### 商家维度的复购

```sql
SELECT merchant_id, buy_user_count, repeat_buy_user_count, repeat_buy_user_rate
FROM tmall_merchant_metrics
ORDER BY repeat_buy_user_rate DESC LIMIT 10
```

`repeat_buy_user_count` 是**在该商家有过 2 条及以上购买行为**的用户数，
`repeat_buy_user_rate` 是它占该商家 `buy_user_count` 的比例。

分母用购买用户数、不用全部行为用户数：复购率问的是
「在这个商家买过的人里有多少人买了不止一次」，
把从没买过的人放进分母会得到一个偏低的数。

**类目维度没有复购指标。** 同一个类目下的两次购买可能来自两家不同的店，
那是品类偏好而不是复购。给类目也算一个「复购率」会造出一个
听起来合理、实际无法解释的数字。

### 复购样本规模

```sql
SELECT dataset_split, label_group, sample_count, positive_rate
FROM tmall_repurchase_metrics
```

回答「训练集有多少个用户×商家样本、其中正样本占多少」。
`positive_rate` 只有 train 集有值。`average_probability` 通常为 `NULL`
（本地这一版 test 集的 `prob` 整列为空）。

**`positive_rate` 在 train 的两行上存的是同一个数。** 它是整个 train 集的属性，
不是某一行的属性，所以 `positive` 与 `negative` 两行给出的是同一个值。
查询时请显式写全条件：

```sql
SELECT positive_rate FROM tmall_repurchase_metrics
WHERE dataset_split = 'train' AND label_group = 'positive'
```

**不要只写 `WHERE dataset_split = 'train'` 就去取第一个值。**
虽然现在两行同值（怎么取都对），但把口径写完整是唯一能防止
「将来有人改回按组各算、negative 行变成 0」的写法——
而那个 0 看起来完全像个合法答案。

## 3. 复购、购买广度、预测标签：三个概念不能混

这三个词在日常对话里都可能被叫作「复购」，但它们的含义完全不同。

| | 复购 | 购买广度 | train 集的 `label` |
| --- | --- | --- | --- |
| 字段 | `repeat_buy_flag`<br>`repeat_buy_user_count` | `multi_merchant_buy_flag`<br>`buy_merchant_count` | `tmall_repurchase_samples.label` |
| 含义 | 在**同一个商家**买过 ≥2 次 | 在 **≥2 个不同商家**买过 | **未来**是否会在该商家复购 |
| 时间口径 | 已经发生的事实 | 已经发生的事实 | 尚未发生的预测目标 |

### 复购 ≠ 购买广度

这是最容易搞反的一处：

- 在 10 家店各买 1 次的人：**广度很高，复购为零**；
- 只在 1 家店买了 5 次的人：**广度为零，却是典型的复购用户**。

把两者混为一谈，所有复购相关的结论都会反过来。
早先的实现正是把「≥2 个不同商家」当成了复购，那是个口径错误。

### 复购 ≠ 预测标签

`repeat_buy_flag` 为真，**不等于** `label` 会是 1。

前者说的是「他以前在同一家店买过不止一次」，是**历史行为**；
后者说的是「他以后还会不会来」，是**待预测的目标**。
用前者去解释后者，等于用「他以前买得多」证明「他以后还会来」——
这是一个假设，不是数据结论。

## 4. 常见问法与推荐口径

| 问法 | 推荐口径 | 需要附带说明 |
| --- | --- | --- |
| 天猫这段时间有多少行为 | `SUM(tmall_funnel_metrics.event_count)` | 单位是行为记录条数，不是订单数 |
| 各环节分别有多少人 | `tmall_funnel_metrics.user_count` | 用户级去重，不是 session 漏斗 |
| 点击到购买的比例 | `tmall_funnel_metrics.user_rate`（buy 行） | 是行为占比，不是转化率 |
| 哪个商家购买用户最多 | `tmall_merchant_metrics` 排行 | 只有 ID，没有商家名称 |
| 哪个商家复购率最高 | `tmall_merchant_metrics.repeat_buy_user_rate` | 复购 = 同一商家买过 ≥2 次；buy 是行为记录不是订单 |
| 用户购买面宽不宽 | `tmall_user_metrics.multi_merchant_buy_flag` | 这是**购买广度**，不是复购 |
| 行为量随时间怎么变 | `tmall_daily_metrics` 按 `metric_date` | 含双十一脉冲，不是自然增长 |
| 复购样本有多少 | `tmall_repurchase_metrics.sample_count` | test 集没有标签也没有概率 |
| 训练集正样本占比 | `positive_rate`（`train` + `positive`） | train 两行同值；test 为 NULL |
| 天猫的 GMV / 销售额 | **没有这个指标** | 数据里没有价格字段 |
| 天猫的订单量 | **没有这个指标** | 数据里没有订单号 |

## 5. 回答天猫问题时必须带上的限定

只要用到上面的指标，回答里就应当包含对应的限定，不能只给数字：

1. **样本范围**：这是按用户抽样后的子集，不是平台全量。
2. **时间范围**：2014-05-11 ~ 2014-11-12，含双十一大促，不是自然周期。
3. **口径性质**：行为记录数不是订单数；用户占比不是转化率。
4. **缺失信息**：没有价格、订单号、数量、名称、地区、物流与退款。

把数字和限定一起给出去，结论才是可用的。
只给数字，听到的人一定会按自己熟悉的电商口径去理解，
而那个理解在大部分情况下是错的。
