# Cross-Exchange API Verification: Polymarket US vs. Kalshi (BTC 15-Minute Markets)

## 1. Executive Summary & Verification Purpose
This document provides the authoritative, verified technical reference for integrating **Polymarket US (`polymarket.us`)** and **Kalshi (`kalshi.com`)** for real-money cross-exchange arbitrage on 15-minute Bitcoin (BTC) event contracts.

Every endpoint, authentication routine, payload schema, WebSocket specification, and fee model documented herein has been inspected and validated against current official documentation, the official SDKs (`polymarket-us` v2.1.0 and `kalshi-python` v2.1.4), and live network queries executed against both exchanges' production gateways.

---

## 2. Official Documentation Reference URLs

### Polymarket US (`polymarket.us`)
- **Developer Documentation Portal:** [https://polymarketusdocs.mintlify.app/](https://polymarketusdocs.mintlify.app/)
- **Developer API Key Management:** [https://polymarket.us/developer](https://polymarket.us/developer)
- **Official Python SDK:** [https://github.com/Polymarket/polymarket-us-python](https://github.com/Polymarket/polymarket-us-python) (`pip install polymarket-us`)
- **REST Base URLs:**
  - Gateway API: `https://gateway.polymarket.us`
  - Trading API: `https://api.polymarket.us`
- **WebSocket Base URL:**
  - `wss://api.polymarket.us`

### Kalshi (`kalshi.com`)
- **Developer Documentation Portal:** [https://docs.kalshi.com](https://docs.kalshi.com)
- **Official Python SDK:** [https://github.com/Kalshi/kalshi-python](https://github.com/Kalshi/kalshi-python) (`pip install kalshi-python`)
- **REST Base URLs:**
  - Production REST: `https://external-api.kalshi.com/trade-api/v2`
  - Elections / Public REST: `https://api.elections.kalshi.com/trade-api/v2`
  - Demo REST: `https://external-api.demo.kalshi.co/trade-api/v2`
- **WebSocket Base URLs:**
  - Production WebSocket: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
  - Demo WebSocket: `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`

---

## 3. BTC 15-Minute Market Identity & Settlement Mechanics

### Polymarket US BTC 15-Minute Markets
- **Series Identifier:** `btc-updown-15m`
- **Market Slug Pattern:** `cpc-btc-updown-15m-YYYY-MM-DD-HHMMz` (e.g., `cpc-btc-updown-15m-2026-10-03-0700z`)
- **Underlying Asset:** Bitcoin (`btc` / `ASSET_CLASS_CRYPTO`)
- **Market Type:** `ASSET_PRICE_MARKET_TYPE_UP_DOWN`
- **Index Symbol:** `BRTI` (CF Benchmarks Bitcoin Real-Time Index)
- **Duration / Horizon:** `15m` (900 seconds)
- **Reference Price (`priceToBeat`):** Strike value in USD determined by the 60-second average of BRTI prior to window start.
- **Settlement Rule:** Resolves to **Up (Yes)** if the 60-second BRTI price average before window end is greater than or equal to `priceToBeat`. Otherwise, resolves to **Down (No)**.
- **Contract Payout:** $1.00 USD on winning outcome; $0.00 USD on losing outcome.
- **Price Precision:** 2 decimal places ($0.01 tick size).

### Kalshi BTC 15-Minute Markets
- **Series Identifier:** `KXBTC15M`
- **Ticker Pattern:** `KXBTC15M-YYMMDDHHMM-SS` (e.g., `KXBTC15M-26OCT030315-15`)
- **Event Ticker Pattern:** `KXBTC15M-YYMMDDHHMM`
- **Underlying Asset:** Bitcoin (`BTC`)
- **Market Type:** `binary`
- **Index Symbol:** CF Benchmarks Bitcoin Real-Time Index (`BRTI`)
- **Duration / Horizon:** 15 minutes (900 seconds)
- **Floor Strike (`floor_strike`):** Strike value in USD determined by the 60-second average of BRTI prior to interval open.
- **Settlement Rule:** Resolves to **Yes** if the simple average of the 60 seconds of BRTI before window close is at least the strike price. Otherwise resolves to **No**.
- **Contract Payout:** $1.00 USD on winning outcome; $0.00 USD on losing outcome.
- **Price Precision:** Cent-based or decimal dollars ($0.01 tick size, e.g. 1 to 99 cents).

### Critical Market Equivalence Finding
Both exchanges utilize the **identical underlying index (`CF Benchmarks BRTI`)**, the **identical 60-second sampling window**, the **identical strike / opening price (`floor_strike` == `priceToBeat`)**, and the **identical 15-minute start and end timestamps**. Market equivalence can thus be validated mathematically and deterministically.

---

## 4. Authentication Mechanisms

### Polymarket US Authentication
- **Algorithm:** Ed25519 (using `pynacl.signing.SigningKey`)
- **Credentials Required:**
  - `key_id`: UUID string identifying the API key
  - `secret_key`: Base64-encoded Ed25519 private seed (32 bytes)
- **Signing Message:** `{timestamp_ms}{method}{path}`
- **Headers Generated:**
  ```http
  X-PM-Access-Key: <key_id>
  X-PM-Timestamp: <current_epoch_ms>
  X-PM-Signature: <base64_encoded_ed25519_signature>
  ```
- **WebSocket Auth:** Transmitted as HTTP handshake headers (`X-PM-Access-Key`, `X-PM-Timestamp`, `X-PM-Signature`) when initiating `wss://api.polymarket.us/v1/ws/markets` or `/v1/ws/private`.

### Kalshi Authentication
- **Algorithm:** RSA-PSS with SHA-256 (MGF1 with SHA-256, salt length = `PSS.DIGEST_LENGTH`)
- **Credentials Required:**
  - `api_key_id`: Kalshi API Key ID string
  - `private_key`: 2048-bit RSA Private Key in PEM format (or path to PEM file)
- **Signing Message:** `{timestamp_ms}{method_uppercase}{path}` (e.g., `1703123456789GET/trade-api/v2/portfolio/balance`)
- **Headers Generated:**
  ```http
  KALSHI-ACCESS-KEY: <api_key_id>
  KALSHI-ACCESS-TIMESTAMP: <current_epoch_ms>
  KALSHI-ACCESS-SIGNATURE: <base64_encoded_rsa_pss_signature>
  ```
- **WebSocket Auth:** Passed as handshake headers (`KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`) to `wss://external-api-ws.kalshi.com/trade-api/ws/v2`.

---

## 5. API Endpoints & Schemas

### Polymarket US REST Endpoints
| Purpose | Method | Path | Authentication | Notes |
| :--- | :--- | :--- | :--- | :--- |
| Market Search / Discovery | `GET` | `/v1/search?query=BTC` | No | Public gateway search |
| Markets List | `GET` | `/v1/markets` | No | Public gateway list |
| Market BBO | `GET` | `/v1/markets/{slug}/bbo` | No | Best Bid & Offer + askShares/bidShares |
| Account Balances | `GET` | `/v1/account/balances` | Yes | Cash balance |
| Portfolio Positions | `GET` | `/v1/portfolio/positions` | Yes | Active positions |
| Create Order | `POST` | `/v1/orders` | Yes | Order submission |
| Order Preview | `POST` | `/v1/order/preview` | Yes | Dry-run preflight check |
| Open Orders | `GET` | `/v1/orders/open` | Yes | List open orders |
| Cancel Order | `POST` | `/v1/order/{order_id}/cancel` | Yes | Cancel single order |
| Cancel All Orders | `POST` | `/v1/orders/open/cancel` | Yes | Emergency cancel |
| Close Position | `POST` | `/v1/order/close-position` | Yes | Emergency position unwind |

#### Polymarket US Order Submission Payload
```json
{
  "marketSlug": "cpc-btc-updown-15m-2026-10-03-0700z",
  "intent": "ORDER_INTENT_BUY_LONG",
  "type": "ORDER_TYPE_LIMIT",
  "price": {
    "value": "0.6200",
    "currency": "USD"
  },
  "quantity": 1,
  "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
  "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
  "synchronousExecution": true
}
```
*Note: `ORDER_INTENT_BUY_LONG` buys YES; `ORDER_INTENT_BUY_SHORT` buys NO.*

### Kalshi REST Endpoints
| Purpose | Method | Path | Authentication | Notes |
| :--- | :--- | :--- | :--- | :--- |
| Market Discovery | `GET` | `/trade-api/v2/markets?series_ticker=KXBTC15M&status=open` | Optional | Returns active/open markets |
| Market Orderbook | `GET` | `/trade-api/v2/markets/{ticker}/orderbook` | Optional | Full bid ladders |
| Account Balance | `GET` | `/trade-api/v2/portfolio/balance` | Yes | Cash balance in cents |
| Portfolio Positions | `GET` | `/trade-api/v2/portfolio/positions` | Yes | Active contract positions |
| Create Order | `POST` | `/trade-api/v2/portfolio/orders` | Yes | Order placement |
| Cancel Order | `DELETE`| `/trade-api/v2/portfolio/orders/{order_id}` | Yes | Cancel single order |
| Batch Cancel | `DELETE`| `/trade-api/v2/portfolio/orders` | Yes | Cancel multiple orders |

#### Kalshi Order Submission Payload
```json
{
  "ticker": "KXBTC15M-26OCT030315-15",
  "client_order_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "side": "yes",
  "action": "buy",
  "count": 1,
  "type": "limit",
  "yes_price": 63,
  "expiration_ts": 1727939700
}
```
*Note: On Kalshi, price is in cents (1 to 99) or dollars. To buy NO, `side="no"`, `no_price=38`.*

---

## 6. WebSocket Streams & Order Book Mechanics

### Polymarket US WebSocket
- **Endpoint:** `wss://api.polymarket.us/v1/ws/markets`
- **Subscription Request:**
  ```json
  {
    "subscribe": {
      "requestId": "sub-1",
      "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
      "marketSlugs": ["cpc-btc-updown-15m-2026-10-03-0700z"]
    }
  }
  ```
- **Incoming Message Structure:**
  - Snapshot: contains `bids` and `offers` as `[{"px": {"value": "0.6200", "currency": "USD"}, "qty": "100"}, ...]`.
  - Also emits `heartbeat` every 15s.

### Kalshi WebSocket
- **Endpoint:** `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
- **Subscription Request:**
  ```json
  {
    "id": 1,
    "cmd": "subscribe",
    "params": {
      "channels": ["orderbook_delta"],
      "market_tickers": ["KXBTC15M-26OCT030315-15"]
    }
  }
  ```
- **Bids-Only Model & Ask Derivation:**
  Kalshi maintains resting bid ladders:
  - `yes_dollars_fp`: list of `[price_dollars, count_fp]` (bids to buy YES).
  - `no_dollars_fp`: list of `[price_dollars, count_fp]` (bids to buy NO).
  - **Derivation of Asks:**
    - To BUY YES (marketable buy YES): matches against resting NO bids.
      $$\text{Best YES Ask} = 1.00 - \max(\text{no\_bids})$$
      $$\text{Available YES Ask Size} = \text{size at } \max(\text{no\_bids})$$
    - To BUY NO (marketable buy NO): matches against resting YES bids.
      $$\text{Best NO Ask} = 1.00 - \max(\text{yes\_bids})$$
      $$\text{Available NO Ask Size} = \text{size at } \max(\text{yes\_bids})$$

---

## 7. Fee Calculations

### Kalshi Official Taker Fee Formula
Kalshi uses a parabolic taker fee formula based on probability uncertainty:
$$\text{Fee}_{\text{Kalshi}} = \left\lceil 0.07 \times C \times P \times (1 - P) \right\rceil \quad (\text{rounded up to the nearest cent})$$
Where:
- $C$ = Contract count (default 1)
- $P$ = Contract price in dollars ($0.01 \le P \le 0.99$)

*Example: For $P = 0.50$, Fee $= \lceil 0.07 \times 1 \times 0.25 \rceil = \lceil 0.0175 \rceil = \$0.02$.*

### Polymarket US Taker Fee Formula
Polymarket US applies a dynamic taker fee on 15-minute crypto markets scaled by the `feeCoefficient` exposed in market metadata:
$$\text{Fee}_{\text{Poly}} = \text{round}\Big(\text{feeCoefficient} \times C \times P \times (1 - P), 4\Big)$$
From live API metadata, `feeCoefficient = 0.0695` (~0.07). Maker orders are 0% (free).
To guarantee positive EV, the bot enforces a conservative ceiling:
$$\text{Fee}_{\text{Poly, conservative}} = \max\Big(\$0.01, \left\lceil 0.07 \times C \times P \times (1 - P) \right\rceil\Big)$$

---

## 8. Rate Limits & Concurrency Guidelines

| Venue | REST Request Limit | Order Submission Limit | WebSocket Constraints | Reconnect Backoff |
| :--- | :--- | :--- | :--- | :--- |
| **Polymarket US** | 20 req / sec | 10 orders / sec | 1 authenticated conn | Exponential (100ms - 5s) |
| **Kalshi** | 10 req / sec | 10 orders / sec | 1 authenticated conn | Exponential (200ms - 10s) |

---

## 9. Discrepancies & SDK Implementation Notes
1. **SDK Authentication:** The official `kalshi-python` library loads private keys from file paths (`private_key_path`). Our high-performance adapter supports both direct in-memory PEM string loading (`KALSHI_PRIVATE_KEY_PEM`) and file path loading (`KALSHI_PRIVATE_KEY_PATH`).
2. **Polymarket US Gateway vs Trading URL:** Public discovery endpoints (`/v1/search`, `/v1/markets`, `/v1/markets/{slug}/bbo`) are served by `gateway.polymarket.us`, whereas trading endpoints (`/v1/orders`, `/v1/account`, `/v1/portfolio`) require `api.polymarket.us`. The bot's client automatically routes requests to the proper host.
3. **Data Mode / Paper Mode:** In unauthenticated or data-only mode, the system queries the public endpoints without requiring API keys, allowing full inspection and validation prior to live trading credential setup.
