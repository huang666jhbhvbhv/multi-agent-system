"""简单的四则运算模块。"""


def add(a, b):
    """返回 a + b。"""
    return a + b


def subtract(a, b):
    """返回 a - b。"""
    return a - b


def multiply(a, b):
    """返回 a * b。"""
    return a * b


def divide(a, b):
    """返回 a / b，b 为 0 时抛出 ValueError。"""
    if b == 0:
        raise ValueError("除数不能为 0")
    return a / b
