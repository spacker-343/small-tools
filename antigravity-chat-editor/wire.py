"""Lossless protobuf wire editing for the locally inspected Antigravity schema."""
from dataclasses import dataclass


def varint(data, pos=0):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError('截断的 protobuf varint')
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            if shift == 63 and byte > 1:
                raise ValueError('protobuf varint 溢出')
            return value, pos
    raise ValueError('protobuf varint 过长')


def encode_varint(value):
    if value < 0:
        raise ValueError('不支持负 varint')
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


@dataclass(frozen=True)
class Field:
    number: int
    wire: int
    value: object
    raw: bytes


def fields(data):
    pos = 0
    result = []
    while pos < len(data):
        start = pos
        tag, pos = varint(data, pos)
        number, kind = tag >> 3, tag & 7
        if number < 1 or number >= 1 << 29:
            raise ValueError('无效 protobuf 字段号')
        if kind == 0:
            value, pos = varint(data, pos)
        elif kind == 2:
            size, pos = varint(data, pos)
            value = data[pos:pos + size]
            pos += size
        elif kind in (1, 5):
            size = 8 if kind == 1 else 4
            value = data[pos:pos + size]
            pos += size
        else:
            raise ValueError('未知 protobuf wire type，停止修改')
        if pos > len(data):
            raise ValueError('截断的 protobuf 字段')
        result.append(Field(number, kind, value, data[start:pos]))
    return result


def one(data, number, default=None, kind=None):
    found = [field for field in fields(data) if field.number == number]
    if len(found) > 1:
        raise ValueError(f'字段 {number} 重复，停止修改')
    if not found:
        return default
    if kind is not None and found[0].wire != kind:
        raise ValueError(f'字段 {number} 类型不兼容')
    return found[0].value


def blob_field(number, value):
    return encode_varint(number << 3 | 2) + encode_varint(len(value)) + value


def rewrite(data, replacements):
    """Replace length-delimited fields or remove fields; retain all others verbatim."""
    output, seen = [], set()
    for field in fields(data):
        if field.number not in replacements:
            output.append(field.raw)
            continue
        if field.number in seen:
            raise ValueError('待修改字段重复')
        seen.add(field.number)
        value = replacements[field.number]
        if value is not None:
            if field.wire != 2:
                raise ValueError('待修改字段类型不兼容')
            output.append(blob_field(field.number, value))
    for number, value in replacements.items():
        if number not in seen and value is not None:
            output.append(blob_field(number, value))
    return b''.join(output)
