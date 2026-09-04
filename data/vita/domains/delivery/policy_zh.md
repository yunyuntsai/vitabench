# 外卖配送时间处理规程（delivery 域）

本文件为 delivery 域的附加策略，仅在 VITA_POLICY_MODE=routed 时由 loader 按需注入到
agent 系统提示中。路由只依据用户可见信号（用户画像 user_profile + 用户指令 instructions
+ 实时对话），绝不依据 evaluation_criteria / rubrics / required_orders。无匹配时回退到
不注入（即基准 agent，安全降级）。

## delivery-time

当用户对送达时间有明确要求时（例如"在X点前/后/左右/之间送达"、"提前一小时送达"、"按时送达"、"不要迟到"、"赶着吃"等），必须按以下流程确定 create_delivery_order 的 dispatch_time：

1. dispatch_time 是骑手从商家取餐**出发**的时间，**不是送达时间**。实际送达时间 = dispatch_time + 配送时长。
2. 先调用 delivery_distance_to_time，按"商家到用户配送地址的距离"估算配送时长（分钟）。
3. 依据用户的时间要求，选择 dispatch_time 使「dispatch_time + 配送时长」满足该要求：
   - 要求"在 T 之前送达"：dispatch_time = T − 配送时长（留出余量，宁可提前几分钟到达）。
   - 要求"在 T 左右送达"：dispatch_time = T − 配送时长（送达时间≈T）。
   - 要求"在 T 之后送达"：dispatch_time = T − 配送时长（送达时间会落在 T 之后）。
   - 要求"在 T1 至 T2 之间送达"：选择 dispatch_time 使送达时间落在 [T1, T2] 区间内，例如 dispatch_time = T1 − 配送时长（送达≈T1，落在区间起点），或取区间中点 (T1+T2)/2 − 配送时长。
4. **多个订单各自有独立的 dispatch_time**，应分别按每个订单自己的送达时间要求与配送时长计算，切勿混用或用同一时间覆盖所有订单。
5. **切勿把 dispatch_time 直接设为用户期望的送达时间**——那样实际送达会晚一个配送时长。也不要把时间要求写进订单备注（note 用于饮食禁忌等信息）。
6. 下单前向用户口述确认"预计送达时间"，确保其满足用户的时间要求；若算出的送达时间无法满足，应调整 dispatch_time 或与用户协商，而非照原值下单。
