# 技能接入协议

## 注册契约

注册文件包含 adapter（http）、endpoint、input_schema、output_schema、checks、resources、cancelable、replay_safe；verifier 标记此技能能进行目标验证。可选 token_env、timeout、execution_timeout。资源可用未加前缀的名称、当前机器人完整名称或 shared/ 前缀；拒绝折算后重名和跨机器人引用。模型只能使用已经注册的能力。

checks 支持非空 nonempty、类型一致相等 eq、数值 gte/lte；pa