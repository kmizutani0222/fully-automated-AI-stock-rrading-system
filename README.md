# AI Trading Platform

日本株と米国株を、同じデータ基盤で扱う自動売買システムです。Phase 1 は5分足の取得と品質検査、Phase 2 はルールベースのペーパートレードとバックテストです。実注文、AI、Web画面はありません。

取引モードは `PAPER` だけです。`LIVE_TRADING_ENABLED=true` または `TRADING_MODE=LIVE` ではプロセスが起動を拒否します。ブローカー実装は `app/broker` のシミュレーションだけで、証券会社への送信コードはありません。

## 起動

```bash
docker compose up -d --build
```

API は http://localhost:8088 です。PostgreSQL はホストの `localhost:55432` に出します。コンテナ内では `db:5432` です。このマシンでは 5432 と 8000 が他のサービスで使われているため、公開ポートをずらしています。

```bash
curl http://localhost:8088/health
curl http://localhost:8088/instruments
```

`GET /docs` にAPIの一覧があります。

## 環境変数

`.env.example` を `.env` にコピーして使います。Compose 内のアプリは `docker-compose.yml` の `DATABASE_URL` を使うので、コンテナ起動自体に `.env` は不要です。

`LIVE_TRADING_ENABLED=true` または `TRADING_MODE=LIVE` ではプロセスが起動を拒否します。実売買ブローカーは実装していません。

## マイグレーションと銘柄

コンテナ起動時に Alembic と銘柄登録が走ります。単体で実行する場合は次です。

```bash
docker compose exec app alembic upgrade head
docker compose exec app python scripts/seed_instruments.py
```

初期銘柄は `config/instruments.yaml` の AAPL、MSFT、NVDA、SPY、QQQ です。銘柄の追加は `POST /instruments` でもできます。

## CSVインポート

必須列は `market,symbol,timestamp,open,high,low,close` です。`volume,vwap,trade_count,timeframe` は任意です。タイムゾーン付きの時刻はそのオフセットを使い、付いていない時刻は `--timezone` のタイムゾーンとして解釈してから UTC で保存します。

```bash
docker compose exec app python scripts/import_csv.py data/samples/us_5m_example.csv
```

不正な価格は保存せず、その行があるとコマンドは終了コード 1 です。同じ銘柄・時刻・プロバイダの再投入は上書きで、行は増えません。欠けた5分足は作りません。`GAP` として表示し、`GET /data-quality/{symbol}` でも返します。データベースへ接続できない場合も終了コードは 0 になりません。`GET /health` はデータベース障害のとき HTTP 503 です。

## 米国株の取得

無料の Yahoo Finance アダプターです。APIキーは不要です。5分足は直近およそ60日までです。

```bash
docker compose exec app python scripts/collect_us_bars.py --symbols AAPL --days 5
```

取得に失敗した銘柄はスキップします。失敗時にモック価格をデータベースへ入れることはありません。モックはテストと、`MARKET_DATA_PROVIDER=mock` または `--provider mock` を明示したときだけ使います。

## データ品質

`GET /data-quality/{symbol}` は保存済みの5分足について、同一取引日の中の欠けを返します。日本株の 11:30 から 12:30 の昼休みは欠けとして扱いません。休日に足が無いことは、比較する足が無いので欠けにはしません。

## テスト

```bash
docker compose exec app pytest -q
```

PostgreSQL が無い環境では、データベースを使うテストだけスキップします。

## Phase 2 のバックテスト

戦略は `config/trading.yaml` でパラメータを変えます。実装しているのは次の3つです。

- `ema_cross`: EMA 9 と EMA 21。上抜けで買い候補、下抜けで決済候補
- `rsi_reversion`: RSI 14。30未満から30以上への回復で買い候補、70以上で決済候補
- `buy_hold`: 最初の確定足で買い、終了時は未決済なら時価評価

いずれもロングのみです。シグナル足の終値では約定せず、次の5分足の始値で約定します。次の足が無い場合はそのシグナルを見送ります。1本の足で損切りと利確の両方に届いた場合は損切りを採用します。

初期の仮想資金は円 1,000,000 と米ドル 0 です。米国株の検証では `initial_cash` に USD を明示します。保存済みの為替レートが無い通貨は自動換算しません。

```bash
curl -X POST http://localhost:8088/api/v1/backtests \
  -H 'Content-Type: application/json' \
  -d '{"strategy":"buy_hold","market":"US","symbols":["AAPL"],"start":"2026-10-01T00:00:00Z","end":"2026-10-14T00:00:00Z","provider":"yfinance","initial_cash":{"USD":"10000"}}'
curl http://localhost:8088/api/v1/backtests/1
curl http://localhost:8088/api/v1/strategies
curl http://localhost:8088/api/v1/portfolio
```

`POST /api/v1/backtests` は 202 と `PENDING` を返します。完了後の状態は `SUCCEEDED` または `FAILED` です。データが足りない実行は失敗となり、指標を 0 で埋めません。計算できない指標は `null` です。

## 未実装

Phase 3 以降です。今回は作っていません。

- AIモデルの学習、予測、戦略の自動切り替え
- ウォークフォワードとスケジューラ
- 証券会社API、LIVE取引、実資金の入出金
- 信用取引、レバレッジ、空売り
- Next.js の管理画面
- 日本株のオンライン取得。日本株は CSV 投入だけです
