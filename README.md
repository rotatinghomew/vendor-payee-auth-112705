# 供应商收款账户确认与在途付款保护

农资结算参与方发布的演练资料。各结算端分别保存自己观察到的账户与付款事实。

## 数据接口

各来源用 UTF-8 的 JSON 数组交付，各文件保留来源自己的标识。索引给出各路资料的归属。逗号连接的字段表示同一记录中的多项取值，`null` 表示来源没有提供相应信息。时间字段采用索引所列起点后的分钟数；事件先后按相关来源给出的时刻理解，数组位置仅用于陈列。所有人员、机构和业务编号仅供本地演练。
字段名用原系统的英文约定。请求状态以各端交付的原始记录为准，包含证据引用的字段并非可用凭据。

| 文件 | 来源 | 约定 |
| --- | --- | --- |
| `mandates.json` | 公司目录 | contact 表示已核实的联系途径编号，不是联系方式正文。 |
| `bank_facts.json` | 开户行 | currencies 分别列出获开户行确认的结算币种；更正引用原事实编号。 |
| `invoices.json` | 财务登记 | 金额按分登记；有保理安排时账单给出法律收款人和协议编号。 |
| `factoring.json` | 债权登记处 | 每份保理以明确的账单及币种为准。 |
| `pay_receipts.json` | 款项处理方 | 受理和扣款为独立事实；declined 表示明确未执行。 |
| `change_reviews.json` | 已认证维护端 | representative 为目录中的请求来源，decision 记录发起或核准。 |

## 本地流程

`settlement.py` 在本地完成换账受理、核验与付款办理结论(只读资料,不实际划款):

```sh
python3 settlement.py changes                  # 换账请求的受理与核验结论
python3 settlement.py accounts                 # 当前已核准的收款关系及账户依据
python3 settlement.py pay                      # 值班员办理视图:照付 / 待核 / 误付待追偿
python3 settlement.py submit --invoice '{...}' # 合作社财务提交新合成发票前核查
python3 settlement.py supplier --person u30    # 供应商维护视图(仅有权代表的法人)
python3 settlement.py ticket --invoice i-5     # 公共票据状态(只引用支付编号)
```

各子命令支持 `--at 分钟` 指定评估时刻(默认取资料中最晚观察时刻)与 `--json` 机器输出。
办理口径:换账须发起与复核分离、渠道已登记且仍有效,并由开户行确认账户归属与币种;
已扣款只按原指令跟进,受理中不得转投新账,明确未执行才可重选当前有效账户;经更正
从未属于拟收款法人的账户,已付款项列作误付待追偿并保留原单据,不自动再次支付。

## 本地运行

使用 Python 3.11 标准库。启动只读资料服务后可查看各来源当前准备好的输入文件：

```sh
python3 -m http.server 8765 --bind 127.0.0.1 --directory feeds
```

资料目录为 `http://127.0.0.1:8765/index.json`。

```sh
python3 -m unittest discover -s tests -v
```

测试核对演练资料的字段、索引及来源间的引用。
