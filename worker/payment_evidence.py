"""Recover a current task's known order without treating payment as verified."""
from pathlib import Path

from parallel_checkout import ORDER_NUMBER, _read


def current_unverified_order(task_dir, *, submitted):
    """Return only the public order identity from this submitted task's record."""
    if submitted is not True:
        return None
    try:
        payment = _read(Path(task_dir) / 'payment.json')
    except (OSError, TypeError, ValueError):
        return None
    number = payment.get('order_number')
    if (payment.get('status') != 'verification_required' or payment.get('success') is not False
            or not isinstance(number, str) or ORDER_NUMBER.fullmatch(number) is None):
        return None
    # The payment reference and amount have not passed verification. In
    # particular, do not return the payment URL, QR, or a success indicator.
    return {'success': False, 'status': 'verification_required', 'order_number': number,
            'order_submitted': True}
