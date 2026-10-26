"""金额转换：人民币大写，供报价 PDF、Excel 与页面共用。"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

DIGITS = "零壹贰叁肆伍陆柒捌玖"
SMALL_UNITS = ("", "拾", "佰", "仟")
BIG_UNITS = ("", "万", "亿", "兆")
QUANTITY_STEP = Decimal("0.001")


def format_quantity(value) -> str:
    """客户页面/PDF 的长度显示精度：毫米级，最多 3 位小数。

    计算和 JSON 仍保留原始精度；这里只负责展示，避免把 CAD 的
    47.23456789 米直接印在报价单上。
    """
    try:
        number = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return str(value)
    number = number.quantize(QUANTITY_STEP, rounding=ROUND_HALF_UP)
    if number == number.to_integral_value():
        return str(int(number))
    return f"{number.normalize():f}".rstrip("0").rstrip(".")


def _four_digits(value: int) -> str:
    parts: list[str] = []
    zero_pending = False
    for position in range(3, -1, -1):
        digit = (value // (10**position)) % 10
        if digit:
            if zero_pending and parts:
                parts.append("零")
            parts.append(DIGITS[digit] + SMALL_UNITS[position])
            zero_pending = False
        elif parts:
            zero_pending = True
    return "".join(parts)


def upper_amount(value: Decimal | str | int | float) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    negative = amount < 0
    amount = abs(amount)
    integer = int(amount)
    fraction = int((amount - integer) * 100)

    if integer:
        groups: list[int] = []
        remaining = integer
        while remaining:
            groups.append(remaining % 10000)
            remaining //= 10000
        parts: list[str] = []
        zero_pending = False
        for index in range(len(groups) - 1, -1, -1):
            group = groups[index]
            if group == 0:
                if parts:
                    zero_pending = True
                continue
            if parts and (zero_pending or group < 1000):
                if not parts[-1].endswith("零"):
                    parts.append("零")
            parts.append(_four_digits(group) + BIG_UNITS[index])
            zero_pending = False
        integer_text = "".join(parts) or "零"
    else:
        integer_text = "零"

    result = integer_text + "元"
    if fraction == 0:
        result += "整"
    else:
        jiao, fen = divmod(fraction, 10)
        if jiao:
            result += DIGITS[jiao] + "角"
        elif fen:
            result += "零"
        if fen:
            result += DIGITS[fen] + "分"
    return ("负" if negative else "") + result


__all__ = ["format_quantity", "upper_amount"]
