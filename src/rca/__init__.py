# Agent 侧代码（认知层）
#
# 与 world/（被诊断系统）严格分开：
#   world/    被观察的对象，Agent 绝不能修改它
#   src/rca/  Agent 自己：感知（遥测降维）、判断（多 Agent 协作）、行动（受策略约束）
