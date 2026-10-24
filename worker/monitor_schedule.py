"""Daily anonymous monitoring windows, independent of the server's local timezone."""
from datetime import datetime, timedelta
import re
import time
from zoneinfo import ZoneInfo

TIMEZONE = 'Asia/Shanghai'
SHANGHAI = ZoneInfo(TIMEZONE)


def default_schedule():
    return {'enabled': False, 'mode': 'exclude', 'start': '01:00', 'end': '05:00', 'timezone': TIMEZONE}


def validate_schedule(value):
    if not isinstance(value, dict):
        raise ValueError('监控时段配置格式无效。')
    result = dict(default_schedule(), **value)
    if type(result['enabled']) is not bool:
        raise ValueError('监控时段开关必须为布尔值。')
    if result['mode'] not in ('exclude', 'include'):
        raise ValueError('监控时段模式无效。')
    for field in ('start', 'end'):
        if not isinstance(result[field], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', result[field]):
            raise ValueError('监控时段时间必须为 HH:MM。')
    if result['timezone'] != TIMEZONE:
        raise ValueError('监控时段时区必须为 Asia/Shanghai。')
    if result['enabled'] and result['start'] == result['end']:
        raise ValueError('监控时段的开始和结束时间不能相同。')
    return {key: result[key] for key in default_schedule()}


def schedule_state(schedule, timestamp=None):
    result = {'paused': False, 'reason': None, 'next_transition_at_ms': None, 'timezone': TIMEZONE}
    if not schedule['enabled']:
        return result
    now = datetime.fromtimestamp(time.time() if timestamp is None else timestamp, SHANGHAI)
    start, end = (int(value[:2]) * 60 + int(value[3:]) for value in (schedule['start'], schedule['end']))
    minute = now.hour * 60 + now.minute
    inside = start <= minute < end if start < end else minute >= start or minute < end
    paused = inside if schedule['mode'] == 'exclude' else not inside
    transitions = []
    for boundary in (start, end):
        at = now.replace(hour=boundary // 60, minute=boundary % 60, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        transitions.append(at.timestamp())
    result.update(paused=paused, reason=('excluded_window' if schedule['mode'] == 'exclude' else 'outside_window') if paused else None,
                  next_transition_at_ms=int(min(transitions) * 1000))
    return result
