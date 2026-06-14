# 入站上下文压缩模块
# 核心职责：拦截上游请求上下文，利用 headroom 进行智能压缩，降低 Token 消耗
import logging

try:
    import headroom
    HAS_HEADROOM = True
except ImportError:
    HAS_HEADROOM = False
    logging.warning("⚠️ 未检测到 headroom 库，将跳过上下文压缩功能。请考虑执行: pip install headroom")

def compress_messages(messages):
    """
    对传入的上下文消息列表进行压缩处理
    :param messages: 原始的 OpenAI/Anthropic 格式的消息列表
    :return: 压缩后的消息列表
    """
    if not HAS_HEADROOM:
        return messages
        
    try:
        if hasattr(headroom, 'compress'):
            compressed_msgs = headroom.compress(messages)
        elif hasattr(headroom, 'process_messages'):
            compressed_msgs = headroom.process_messages(messages)
        else:
            compressed_msgs = messages
            
        logging.info("🗜️ [Head Room] 上下文压缩处理完成")
        return compressed_msgs
    except Exception as e:
        logging.error(f"⚠️ [Head Room] 上下文压缩失败，已回退至原版上下文: {e}")
        return messages