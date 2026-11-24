"""Detect schema capabilities from the installed server, not its version number."""
import io
import struct
import zipfile

from wire import encode_varint, fields, one, varint


def descriptors(binary, name, tag=34):
    needle = b'\x0a' + encode_varint(len(name)) + name.encode('ascii')
    start = 0
    while (position := binary.find(needle, start)) >= 0:
        start = position + len(needle)
        for before in range(max(0, position - 6), position):
            if binary[before] != tag:
                continue
            try:
                size, offset = varint(binary, before + 1)
                if offset != position or offset + size > len(binary):
                    continue
                value = binary[offset:offset + size]
                if one(value, 1, kind=2) == name.encode('ascii'):
                    yield value
            except ValueError:
                continue


def check_message(binary, name, expected):
    for descriptor in descriptors(binary, name):
        actual = {}
        for field in fields(descriptor):
            if field.number == 2 and field.wire == 2:
                value = field.value
                actual[one(value, 1, kind=2).decode('ascii')] = (one(value, 3, kind=0), one(value, 5, kind=0))
        if all(actual.get(key) == value for key, value in expected.items()):
            return
    raise ValueError(f'当前消息结构不兼容: {name}，需要适配字段，未限制版本号')


def check_enum(binary, name, expected):
    for descriptor in descriptors(binary, name, tag=42):
        actual = {}
        for field in fields(descriptor):
            if field.number == 2 and field.wire == 2:
                actual[one(field.value, 1, kind=2).decode('ascii')] = one(field.value, 2, kind=0)
        if all(actual.get(key) == value for key, value in expected.items()):
            return
    raise ValueError(f'当前状态结构不兼容: {name}')


def renderer_source(binary):
    offset = len(binary)
    while (offset := binary.rfind(b'PK\x05\x06', 0, offset)) >= 0:
        if offset + 22 > len(binary):
            continue
        size, start, comment = struct.unpack_from('<IIH', binary, offset + 12)
        begin, end = offset - size - start, offset + 22 + comment
        if not (0 <= begin < offset and end <= len(binary)) or binary[begin:begin+4] != b'PK\x03\x04':
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(binary[begin:end])) as archive:
                if {'main.js', 'index.html'} <= set(archive.namelist()):
                    return archive.read('main.js').decode('utf-8')
        except zipfile.BadZipFile:
            continue
    raise ValueError('找不到聊天界面资源，当前界面结构需要适配')


def check_server(binary):
    check_message(binary, 'Step', {'type': (1, 14), 'status': (4, 14), 'metadata': (5, 11),
                                   'planner_response': (20, 11)})
    check_message(binary, 'CortexStepPlannerResponse', {'response': (1, 9), 'modified_response': (8, 9),
                    'message_id': (6, 9), 'tool_calls': (7, 11), 'provider_assigned_message_id': (15, 9),
                    'passback_context': (18, 11), 'media': (20, 11)})
    check_message(binary, 'CortexStepMetadata', {'model_api_content_id': (35, 9)})
    check_message(binary, 'CortexStepGeneratorMetadata', {'chat_model': (1, 11)})
    check_message(binary, 'ChatModelMetadata', {'message_prompts': (2, 11)})
    check_message(binary, 'ChatMessagePrompt', {'message_id': (1, 9), 'prompt': (3, 9)})
    check_enum(binary, 'CortexStepType', {'CORTEX_STEP_TYPE_PLANNER_RESPONSE': 15,
                    'CORTEX_STEP_TYPE_CHECKPOINT': 23, 'CORTEX_STEP_TYPE_BRAIN_UPDATE': 55})
    check_enum(binary, 'CortexStepStatus', {'CORTEX_STEP_STATUS_DONE': 3, 'CORTEX_STEP_STATUS_PENDING': 1,
                    'CORTEX_STEP_STATUS_RUNNING': 2, 'CORTEX_STEP_STATUS_GENERATING': 8,
                    'CORTEX_STEP_STATUS_WAITING': 9, 'CORTEX_STEP_STATUS_QUEUED': 11})
    source = renderer_source(binary)
    for anchor in ('data-bubble-side', 'data-step-index', 'plannerResponse', 'sourceTrajectoryStepInfo'):
        if anchor not in source:
            raise ValueError(f'聊天界面缺少结构标记: {anchor}')
    return True
