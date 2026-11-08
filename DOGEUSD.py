"""
LABUSD trading bot for Delta Exchange India (10x leverage, 5% take-profit).

Strategy: Momentum + EMA cross + MACD + Supertrend.
  - BUY  signal: all enabled indicators are bullish
  - SELL signal: all enabled indicators are bearish
  - otherwise: HOLD

Before every new entry the bot:
  1. fetches current open orders and cancels them
  2. fetches the current position and closes it (if it is on the opposite side)
  3. places the new market order

After each entry it sets 10x leverage (once at startup) and places a reduce-only
take-profit limit order (and an optional stop-loss). After a TP/SL exit it waits for a
NEW signal instead of re-entering on the same one.

Signals are evaluated only on CLOSED candles, once per new candle.
Order placements and cancellations are appended to order_history.txt beside this script.

Setup:
  export DELTA_API_KEY=...
  export DELTA_API_SECRET=...
  export DELTA_ENV=testnet        # default. Use "production" for real funds.
  python labusd_bot.py
"""
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode

import requests

# ----------------------------- Config ---------------------------------------
BASE_URLS = {
    'testnet': 'https://cdn-ind.testnet.deltaex.org',
    'production': 'https://api.india.delta.exchange',
}
DELTA_ENV = os.environ.get('DELTA_ENV', 'production').lower()
if DELTA_ENV not in BASE_URLS:
    sys.exit(f'DELTA_ENV must be one of {list(BASE_URLS)}, got "{DELTA_ENV}"')
BASE_URL = BASE_URLS[DELTA_ENV]

API_KEY = 'E0xnVgjxggBS8r7EcxivpvzX7neK9p'
API_SECRET = 'DNLCBMh9jWEUZq4215ZX8gq9VUzJ5OGVK4n30qAhbLVcguBvFKyLXsB2A2o1'
if not API_KEY or not API_SECRET:
    sys.exit('Set DELTA_API_KEY and DELTA_API_SECRET environment variables')

SYMBOL = os.environ.get('SYMBOL', 'DOGEUSD').upper()

LOTS = 200                   # order size in contracts (whole numbers)
TIMEFRAME = '5m'            # 1m,3m,5m,15m,30m,1h,2h,4h,6h,1d
CANDLES = 300               # history used for indicators
POLL_SECONDS = 10

LEVERAGE = 50
TAKE_PROFIT_PERCENT = 20.0   # see TP_MODE
# 'margin': 15% gain on your margin  -> price move of 5% / LEVERAGE = 0.5%
# 'price' : 15% move in the price    -> 50% gain on margin at 10x
TP_MODE = os.environ.get('TP_MODE', 'margin').lower()
STOP_LOSS_PERCENT = float(os.environ.get('STOP_LOSS_PERCENT', '10'))  # same basis as TP; 0 = OFF
REENTER_AFTER_EXIT = False  # False: after TP/SL wait for a new signal before entering again
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'  # log only, no orders
ORDER_HISTORY_FILE = r'C:\Delta\Delta USD\order_history.txt'

# Indicator settings
EMA_FAST, EMA_SLOW = 9, 21
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
MOMENTUM_PERIOD = 10
ST_PERIOD, ST_MULT = 10, 3.0

TF_SECONDS = {'1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
              '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600, '1d': 86400}
if TP_MODE not in ('margin', 'price'):
    sys.exit('TP_MODE must be margin or price')
if TIMEFRAME not in TF_SECONDS:
    sys.exit(f'TIMEFRAME must be one of {list(TF_SECONDS)}')


# ----------------------------- API helpers ----------------------------------
def generate_signature(secret, message):
    return hmac.new(bytes(secret, 'utf-8'), bytes(message, 'utf-8'),
                    hashlib.sha256).hexdigest()


def api_request(method, path, query=None, body=None, auth=False):
    query_string = '?' + urlencode(query, safe=',') if query else ''
    payload = json.dumps(body, separators=(',', ':')) if body else ''
    url = f'{BASE_URL}{path}{query_string}'

    headers = {'Accept': 'application/json', 'User-Agent': 'python-rest-client'}
    if body:
        headers['Content-Type'] = 'application/json'
    if auth:
        timestamp = str(int(time.time()))
        headers.update({
            'api-key': API_KEY,
            'timestamp': timestamp,
            'signature': generate_signature(
                API_SECRET, method + timestamp + path + query_string + payload),
        })

    try:
        response = requests.request(method, url, data=payload or None,
                                    headers=headers, timeout=(3, 27))
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f'HTTP error on {method} {path}: {e} | {e.response.text}')
    except (requests.exceptions.RequestException, ValueError) as e:
        raise RuntimeError(f'Request failed on {method} {path}: {e}')

    if not data.get('success'):
        raise RuntimeError(f'API error on {method} {path}: {data.get("error")}')
    return data


def get_product():
    """Return (product_id, tick_size as Decimal)."""
    data = api_request('GET', f'/v2/products/{SYMBOL}')
    r = data['result']
    return int(r['id']), Decimal(str(r['tick_size']))


def set_leverage(product_id):
    if DRY_RUN:
        print(f'  [DRY RUN] would set leverage to {LEVERAGE}x')
        return
    api_request('POST', f'/v2/products/{product_id}/orders/leverage',
                body={'leverage': LEVERAGE}, auth=True)
    print(f'  Leverage set to {LEVERAGE}x')


def get_candles():
    """Return (times, opens, highs, lows, closes) of CLOSED candles, oldest first."""
    step = TF_SECONDS[TIMEFRAME]
    end = int(time.time())
    start = end - CANDLES * step
    data = api_request('GET', '/v2/history/candles', query={
        'resolution': TIMEFRAME, 'symbol': SYMBOL, 'start': start, 'end': end})
    rows = sorted(data.get('result') or [], key=lambda r: r['time'])
    # drop the still-forming candle
    rows = [r for r in rows if r['time'] + step <= end]
    if len(rows) < max(EMA_SLOW, MACD_SLOW + MACD_SIGNAL, ST_PERIOD) + 5:
        raise RuntimeError(f'Not enough candles returned ({len(rows)})')
    return ([r['time'] for r in rows], [float(r['open']) for r in rows],
            [float(r['high']) for r in rows], [float(r['low']) for r in rows],
            [float(r['close']) for r in rows])


# ----------------------------- Indicators -----------------------------------
def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def macd(closes):
    fast, slow = ema(closes, MACD_FAST), ema(closes, MACD_SLOW)
    line = [f - s for f, s in zip(fast, slow)]
    signal = ema(line, MACD_SIGNAL)
    hist = [m - s for m, s in zip(line, signal)]
    return line, signal, hist


def supertrend(highs, lows, closes, period, mult):
    """Return direction list: 1 = uptrend (bullish), -1 = downtrend, 0 = not ready."""
    n = len(closes)
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
    atr = [None] * n
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    direction = [0] * n
    final_upper = final_lower = None
    for i in range(period - 1, n):
        hl2 = (highs[i] + lows[i]) / 2
        basic_upper, basic_lower = hl2 + mult * atr[i], hl2 - mult * atr[i]
        if final_upper is None:
            final_upper, final_lower = basic_upper, basic_lower
            direction[i] = 1 if closes[i] > hl2 else -1
            continue
        prev_upper, prev_lower, prev_dir = final_upper, final_lower, direction[i - 1]
        final_upper = basic_upper if (basic_upper < prev_upper or closes[i - 1] > prev_upper) else prev_upper
        final_lower = basic_lower if (basic_lower > prev_lower or closes[i - 1] < prev_lower) else prev_lower
        if prev_dir == -1 and closes[i] > prev_upper:
            direction[i] = 1
        elif prev_dir == 1 and closes[i] < prev_lower:
            direction[i] = -1
        else:
            direction[i] = prev_dir
    return direction


def compute_signal(highs, lows, closes):
    """Return ('buy' | 'sell' | 'hold', details dict)."""
    ema_f, ema_s = ema(closes, EMA_FAST), ema(closes, EMA_SLOW)
    macd_line, macd_sig, macd_hist = macd(closes)
    st = supertrend(highs, lows, closes, ST_PERIOD, ST_MULT)
    mom = closes[-1] - closes[-1 - MOMENTUM_PERIOD]

    votes = {
        'ema':        1 if ema_f[-1] > ema_s[-1] else -1,
        'macd':       1 if (macd_line[-1] > macd_sig[-1] and macd_hist[-1] > 0) else
                     (-1 if (macd_line[-1] < macd_sig[-1] and macd_hist[-1] < 0) else 0),
        'momentum':   1 if mom > 0 else (-1 if mom < 0 else 0),
        'supertrend': st[-1],
    }
    if all(v == 1 for v in votes.values()):
        signal = 'buy'
    elif all(v == -1 for v in votes.values()):
        signal = 'sell'
    else:
        signal = 'hold'
    details = {'close': closes[-1], 'ema_fast': round(ema_f[-1], 2),
               'ema_slow': round(ema_s[-1], 2), 'macd_hist': round(macd_hist[-1], 4),
               'momentum': round(mom, 2), 'votes': votes}
    return signal, details


# ----------------------------- Trading actions ------------------------------
def record_order_event(event, **details):
    record = {
        'timestamp': datetime.now().astimezone().isoformat(timespec='seconds'),
        'environment': DELTA_ENV,
        'symbol': SYMBOL,
        'event': event,
        **details,
    }
    with open(ORDER_HISTORY_FILE, 'a', encoding='utf-8') as history_file:
        history_file.write(json.dumps(record, sort_keys=True) + '\n')


def get_open_orders(product_id):
    data = api_request('GET', '/v2/orders', query={
        'product_id': product_id, 'states': 'open,pending'}, auth=True)
    return data.get('result') or []


def cancel_all_orders(product_id):
    orders = get_open_orders(product_id)
    if not orders:
        print('  No open orders to cancel.')
        return
    print(f'  Cancelling {len(orders)} open order(s): '
          f'{[(o.get("id"), o.get("side"), o.get("size")) for o in orders]}')
    if DRY_RUN:
        for order in orders:
            record_order_event(
                'CANCEL_DRY_RUN', order_id=order.get('id'), side=order.get('side'),
                size=order.get('size'), state=order.get('state'))
        return
    api_request('DELETE', '/v2/orders/all', body={'product_id': product_id}, auth=True)
    for order in orders:
        record_order_event(
            'ORDER_CANCELLED', order_id=order.get('id'), side=order.get('side'),
            size=order.get('size'), state='cancelled')


def get_position(product_id):
    """Return (signed_size, entry_price). size > 0 long, < 0 short, 0 flat."""
    data = api_request('GET', '/v2/positions', query={'product_id': product_id}, auth=True)
    result = data.get('result')
    if isinstance(result, list):
        result = result[0] if result else None
    if not result:
        return 0, None
    try:
        size = int(float(result.get('size') or 0))
    except (TypeError, ValueError):
        size = 0
    try:
        entry = float(result.get('entry_price')) if result.get('entry_price') else None
    except (TypeError, ValueError):
        entry = None
    return size, entry


def get_position_size(product_id):
    return get_position(product_id)[0]


def place_market_order(product_id, side, size, reduce_only=False):
    body = {'product_id': product_id, 'size': int(size), 'side': side,
            'order_type': 'market_order'}
    if reduce_only:
        body['reduce_only'] = 'true'
    if DRY_RUN:
        print(f'  [DRY RUN] would place: {body}')
        record_order_event(
            'ORDER_DRY_RUN', side=side, size=int(size), order_type='market_order',
            reduce_only=reduce_only)
        return {}
    result = api_request('POST', '/v2/orders', body=body, auth=True).get('result') or {}
    record_order_event(
        'ORDER_PLACED', order_id=result.get('id'), side=result.get('side', side),
        size=result.get('size', int(size)), order_type='market_order',
        reduce_only=reduce_only, state=result.get('state'))
    print(f'  Order placed: id={result.get("id")} side={result.get("side")} '
          f'size={result.get("size")} state={result.get("state")}')
    return result


def close_position(product_id):
    size = get_position_size(product_id)
    if size == 0:
        print('  No open position to close.')
        return
    side = 'sell' if size > 0 else 'buy'
    print(f'  Closing existing position (size={size}) with a {side} market order...')
    place_market_order(product_id, side, abs(size), reduce_only=True)
    if DRY_RUN:
        return
    for _ in range(10):  # wait until the position is really flat
        time.sleep(1)
        if get_position_size(product_id) == 0:
            print('  Position closed.')
            return
    raise RuntimeError('Position did not close in time; aborting new entry.')


def to_tick(price, tick):
    return format((Decimal(str(price)) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick, 'f')


def move_fraction(percent):
    return percent / 100 / LEVERAGE if TP_MODE == 'margin' else percent / 100


def place_exit_orders(product_id, tick):
    """After an entry: reduce-only take-profit limit (+ optional stop-loss)."""
    size, entry = None, None
    for _ in range(10):                  # wait for the fill to show up as a position
        time.sleep(1)
        size, entry = get_position(product_id)
        if size and entry:
            break
    if not size or not entry:
        print('  WARNING: no position/entry price found; take-profit NOT placed. Check manually!')
        return
    long_ = size > 0
    exit_side = 'sell' if long_ else 'buy'
    sign = 1 if long_ else -1
    tp_price = to_tick(entry * (1 + sign * move_fraction(TAKE_PROFIT_PERCENT)), tick)

    tp_result = api_request('POST', '/v2/orders', body={
        'product_id': product_id, 'size': abs(size), 'side': exit_side,
        'order_type': 'limit_order', 'limit_price': tp_price,
        'time_in_force': 'gtc', 'reduce_only': 'true'}, auth=True).get('result') or {}
    record_order_event(
        'ORDER_PLACED', order_id=tp_result.get('id'), side=exit_side, size=abs(size),
        order_type='limit_order', limit_price=tp_price, reduce_only=True,
        state=tp_result.get('state'))
    print(f'  Take-profit placed: {exit_side} {abs(size)} @ {tp_price} (entry {entry})')

    if STOP_LOSS_PERCENT > 0:
        sl_price = to_tick(entry * (1 - sign * move_fraction(STOP_LOSS_PERCENT)), tick)
        sl_result = api_request('POST', '/v2/orders', body={
            'product_id': product_id, 'size': abs(size), 'side': exit_side,
            'order_type': 'market_order', 'stop_order_type': 'stop_loss_order',
            'stop_price': sl_price, 'reduce_only': 'true'}, auth=True).get('result') or {}
        record_order_event(
            'ORDER_PLACED', order_id=sl_result.get('id'), side=exit_side, size=abs(size),
            order_type='stop_loss_order', stop_price=sl_price, reduce_only=True,
            state=sl_result.get('state'))
        print(f'  Stop-loss placed: {exit_side} {abs(size)} trigger @ {sl_price}')
    else:
        print('  No stop-loss set (STOP_LOSS_PERCENT=0). At 10x the position can be liquidated.')


last_entered = None   # side of the last successful entry; blocks re-entry after TP/SL


def act_on_signal(product_id, tick, signal):
    global last_entered
    if signal == 'hold':
        last_entered = None
        return
    current = get_position_size(product_id)
    already = (signal == 'buy' and current > 0) or (signal == 'sell' and current < 0)
    if already:
        print(f'  Already {"long" if current > 0 else "short"} ({current}); nothing to do.')
        last_entered = signal
        return
    if current == 0 and signal == last_entered and not REENTER_AFTER_EXIT:
        print('  Position already exited (TP/SL) on this signal; waiting for a new signal.')
        return

    print(f'  Signal {signal.upper()}: cleaning up before entry...')
    cancel_all_orders(product_id)      # 1) get current orders (old TP/SL too), cancel them
    close_position(product_id)         # 2) close the existing position first
    print(f'  Placing new {signal.upper()} order...')
    place_market_order(product_id, signal, LOTS)   # 3) place the new order
    last_entered = signal
    if DRY_RUN:
        print('  [DRY RUN] skipping take-profit placement')
        return
    try:
        place_exit_orders(product_id, tick)
    except RuntimeError as e:
        print(f'  WARNING: position is open WITHOUT take-profit: {e}')


# ----------------------------- Main loop ------------------------------------
def main():
    print(f'Environment: {DELTA_ENV} ({BASE_URL}) | {SYMBOL} {TIMEFRAME} | '
          f'lots={LOTS} | leverage={LEVERAGE}x | TP={TAKE_PROFIT_PERCENT}% ({TP_MODE}) | '
          f'SL={STOP_LOSS_PERCENT}% | dry_run={DRY_RUN}')
    if DELTA_ENV == 'production' and not DRY_RUN:
        print('WARNING: PRODUCTION with real funds.')
    product_id, tick = get_product()
    print(f'Product id for {SYMBOL}: {product_id} | tick size {tick}')
    try:
        set_leverage(product_id)
    except RuntimeError as e:
        sys.exit(f'Could not set leverage ({e}). Set {LEVERAGE}x manually in the Delta UI, '
                 'then restart.')

    last_candle_time = None
    while True:
        try:
            times, _, highs, lows, closes = get_candles()
            if times[-1] != last_candle_time:
                last_candle_time = times[-1]
                signal, d = compute_signal(highs, lows, closes)
                ts = datetime.fromtimestamp(times[-1]).strftime('%Y-%m-%d %H:%M')
                print(f'[{ts}] close={d["close"]} signal={signal.upper()} {d}')
                act_on_signal(product_id, tick, signal)
        except RuntimeError as e:
            print(f'ERROR: {e}')
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped. Any open position and its TP/SL orders stay on the exchange.')