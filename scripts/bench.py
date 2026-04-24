import time, requests, sys

def bench(label, source, symbol='NIFTY50', interval='5m'):
    try:
        t0 = time.time()
        r = requests.get(f'http://localhost:5000/api/candles?interval={interval}&symbol={symbol}&source={source}', timeout=30)
        elapsed = time.time() - t0
        d = r.json()
        n = len(d.get('candles', []))
        print(f'{label:40s} | {elapsed:.3f}s | {n} candles')
    except Exception as e:
        print(f'{label:40s} | FAILED: {e}')
    sys.stdout.flush()

print(f'{"Test":40s} | {"Time":>6s} | Candles')
print('-' * 68)

bench('Yahoo Finance | NIFTY50 5m',    'yahoo',       'NIFTY50', '5m')
bench('Yahoo Finance | NIFTY50 15m',   'yahoo',       'NIFTY50', '15m')
bench('Yahoo Finance | NIFTY50 1d',    'yahoo',       'NIFTY50', '1d')
bench('Yahoo Finance | BTC 5m',        'yahoo',       'BTC',     '5m')
bench('Yahoo Finance | BANKNIFTY 5m',  'yahoo',       'BANKNIFTY','5m')
bench('TradingView   | NIFTY50 5m',    'tradingview',  'NIFTY50', '5m')
bench('TradingView   | NIFTY50 15m',   'tradingview',  'NIFTY50', '15m')
bench('TradingView   | NIFTY50 1d',    'tradingview',  'NIFTY50', '1d')
bench('TradingView   | BTC 5m',        'tradingview',  'BTC',     '5m')
bench('TradingView   | BANKNIFTY 5m',  'tradingview',  'BANKNIFTY','5m')
bench('NSE India     | NIFTY50 5m',    'nse',          'NIFTY50', '5m')
bench('NSE India     | BANKNIFTY 5m',  'nse',          'BANKNIFTY','5m')
