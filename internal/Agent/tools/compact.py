"""
压缩策略：
    1.将旧的tool_result替换为占位符，收集所有tool_result的结果，通过tool_name与tool_id进行map配对实现tool_result的匹配
    tool_result保留近3轮tool调用历史，同时对于tool_result结果<100字符的不做占位处理，对于特定tool_result不能做占位

    2.当上下文超过阈值时，先将对话保存到磁盘，然后给LLM做摘要，
    其中转给大模型做摘要时将message全部转化为str并且只保留后key（自定义）个字符给大模型:
    json.dumps(messages, default=str)[:80000]})
"""