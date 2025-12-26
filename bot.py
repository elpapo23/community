import os
import sys
import json
import time
import requests
import datetime
from typing import Optional
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, ApiCreds  # <--- Added ApiCreds
from py_clob_client.order_builder.constants import BUY, SELL

# --- CONFIGURATION ---
BUY_IN_LAST_X_MINUTES = 5
SHARES_TO_BUY = 6.0
BUY_PRICE = 0.99
CHECK_INTERVAL_SECONDS = 5
LOG_FILE_NAME = "bot.log"
RV_OPEN_THR = 0.08
RV_CLOSE_THR = 0.03
RV_COOLDOWN_SECONDS = 120
RV_SHARES = 2.0

def log_message(message):
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    try:
        with open(LOG_FILE_NAME, "a", encoding="utf-8") as f:
            f.write(log_entry + "\n")
    except Exception:
        pass

def load_credentials():
    load_dotenv()
    
    # Wallet Credentials
    private_key = os.getenv("PK") or os.getenv("PRIVATE_KEY")
    funder_address = os.getenv("FUNDER") or os.getenv("FUNDER_ADDRESS")
    
    # API Credentials (THE FIX)
    api_key = os.getenv("POLY_API_KEY")
    api_secret = os.getenv("POLY_API_SECRET")
    api_passphrase = os.getenv("POLY_PASSPHRASE")

    if not private_key or not funder_address:
        log_message("FATAL: Missing PK or FUNDER in .env file.")
        sys.exit(1)
        
    if not (api_key and api_secret and api_passphrase):
        log_message("FATAL: Missing POLY_API_KEY, POLY_API_SECRET, or POLY_PASSPHRASE in .env file.")
        sys.exit(1)

    return private_key, funder_address, api_key, api_secret, api_passphrase

def get_clob_client(private_key, funder_address, api_key, api_secret, api_passphrase):
    host = "https://clob.polymarket.com"
    try:
        # 1. Construct the API Credentials object
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase
        )

        # 2. Pass creds into the Client
        client = ClobClient(
            host=host,
            key=private_key,
            chain_id=137,
            creds=creds,           # <--- AUTHENTICATION ADDED HERE
            signature_type=2,      # Standard for Proxy wallets / API usage
            funder=funder_address
        )
        return client
    except Exception as e:
        log_message(f"FATAL: Client Init Failed. CHECK YOUR CREDENTIALS. {e}")
        sys.exit(1)

def et_now(ts: Optional[float] = None) -> datetime.datetime:
    if ts is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    else:
        now_utc = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return now_utc.astimezone(ZoneInfo("America/New_York"))

def btc_1h_slug_for_now(ts: Optional[float] = None) -> str:
    d = et_now(ts).replace(minute=0, second=0, microsecond=0)
    month = d.strftime("%B").lower()
    day = str(int(d.strftime("%d")))
    hour24 = int(d.strftime("%H"))
    ampm = "am" if hour24 < 12 else "pm"
    hour12 = hour24 % 12
    hour12 = 12 if hour12 == 0 else hour12
    return f"bitcoin-up-or-down-{month}-{day}-{hour12}{ampm}-et"

def place_limit_buy_order(client, token_id, size, price, direction):
    log_message(f"--- Placing LIMIT BUY Order for {direction} ---")
    try:
        order_args = OrderArgs(
            price=float(price),
            size=float(size),
            side=BUY,
            token_id=str(token_id)
        )
        # Sign and Post
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order)
        
        if resp.get("success") is True or "orderID" in resp:
            log_message(f" - ✅ Order ACCEPTED. ID: {resp.get('orderID')}")
            return True
        else:
            err_msg = resp.get('errorMsg') or resp.get('message')
            log_message(f" - ❌ Order FAILED: {err_msg}")
            return False

    except Exception as e:
        log_message(f" - ❌ Exception: {str(e)}")
        return False

def get_polymarket_tokens_for_slug(slug):
    try:
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        response = requests.get(url, timeout=5)

        if response.status_code == 404:
            return None, None, None, None

        market = response.json()

        raw_clob_ids = market.get('clobTokenIds', '[]')
        if isinstance(raw_clob_ids, str):
            clob_token_ids = json.loads(raw_clob_ids)
        else:
            clob_token_ids = raw_clob_ids

        if len(clob_token_ids) == 2:
            return market.get('question'), slug, clob_token_ids[0], clob_token_ids[1]
    except Exception:
        pass
    return None, None, None, None

def get_current_polymarket_tokens():
    # Calculate current 15m interval slug
    current_time = int(time.time())
    # Rounds down to nearest 15 mins (900 seconds)
    market_timestamp = (current_time // 900) * 900
    
    # NOTE: Ensure this slug format matches exactly what Polymarket uses for the day
    slug = f"btc-updown-15m-{market_timestamp}"

    try:
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        response = requests.get(url, timeout=5)
        
        if response.status_code == 404:
            return None, None, None, None
            
        market = response.json()
        
        # Depending on API version, clobTokenIds might be a list or a JSON string
        raw_clob_ids = market.get('clobTokenIds', '[]')
        if isinstance(raw_clob_ids, str):
            clob_token_ids = json.loads(raw_clob_ids)
        else:
            clob_token_ids = raw_clob_ids
        
        if len(clob_token_ids) == 2:
            return market.get('question'), slug, clob_token_ids[0], clob_token_ids[1]
    except Exception:
        pass
    return None, None, None, None

def get_best_bid(token_id):
    try:
        url = f"https://clob.polymarket.com/price?token_id={token_id}&side=SELL"
        data = requests.get(url, timeout=3).json()
        return float(data.get('price', 0))
    except:
        return 0.0

def get_best_ask(token_id):
    try:
        url = f"https://clob.polymarket.com/price?token_id={token_id}&side=BUY"
        data = requests.get(url, timeout=3).json()
        return float(data.get('price', 0))
    except:
        return 0.0

def get_mid_price(token_id):
    bid = get_best_bid(token_id)
    ask = get_best_ask(token_id)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return max(bid, ask, 0.0)

def main():
    log_message("🤖 --- Polymarket Bot Started --- 🤖")
    
    # Load all 5 credentials
    pk, funder, api_key, api_secret, api_passphrase = load_credentials()
    
    # Initialize client with API Creds
    clob_client = get_clob_client(pk, funder, api_key, api_secret, api_passphrase)
    
    log_message(f"Bot configured with Funder: {funder[:6]}...{funder[-4:]}")

    last_trade_interval = -1 
    rv_open = False
    rv_last_action_ts = 0.0
    rv_15m_leg = None
    rv_1h_leg = None

    while True:
        try:
            now = datetime.datetime.now()
            current_interval = now.minute // 15
            
            question, slug, yes_token, no_token = get_current_polymarket_tokens()

            if not slug:
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Market not active yet (Slug: {slug}). Retrying...", end='\r')
                time.sleep(10)
                continue

            # Check prices
            yes_bid = get_best_bid(yes_token)
            no_bid = get_best_bid(no_token)

            # Logic
            minute_in_interval = now.minute % 15
            trigger_minute = 15 - BUY_IN_LAST_X_MINUTES

            if minute_in_interval >= trigger_minute and last_trade_interval != current_interval:
                log_message(f"\n--- WINDOW ACTIVE ({minute_in_interval}/15) ---")
                log_message(f" Market: {question}")
                log_message(f" YES Bid: {yes_bid} | NO Bid: {no_bid}")
                
                success = False
                if yes_bid > no_bid:
                    success = place_limit_buy_order(clob_client, yes_token, SHARES_TO_BUY, BUY_PRICE, "YES")
                elif no_bid > yes_bid:
                    success = place_limit_buy_order(clob_client, no_token, SHARES_TO_BUY, BUY_PRICE, "NO")
                else:
                    log_message(" - Tie. No trade.")

                if success:
                    last_trade_interval = current_interval
            
            elif last_trade_interval == current_interval:
                 print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Trade complete for this block. Waiting...", end='\r')
            else:
                 print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Waiting for window (Current: {minute_in_interval} | Trigger: {trigger_minute})...", end='\r')

            rv_slug = btc_1h_slug_for_now(time.time())
            rv_question, rv_slug, rv_yes_token, rv_no_token = get_polymarket_tokens_for_slug(rv_slug)
            if rv_slug and rv_yes_token and rv_no_token and yes_token and no_token:
                up_mid_15m = get_mid_price(yes_token)
                up_mid_1h = get_mid_price(rv_yes_token)
                signal = up_mid_15m - up_mid_1h

                log_message(f"RV signal: {signal:.4f} (15m: {up_mid_15m:.4f} | 1h: {up_mid_1h:.4f})")

                now_ts = time.time()
                cooldown_active = (now_ts - rv_last_action_ts) < RV_COOLDOWN_SECONDS

                if not rv_open and not cooldown_active and abs(signal) >= RV_OPEN_THR:
                    log_message("--- RV OPEN ---")
                    if signal > 0:
                        rv_15m_leg = ("NO", no_token)
                        rv_1h_leg = ("YES", rv_yes_token)
                    else:
                        rv_15m_leg = ("YES", yes_token)
                        rv_1h_leg = ("NO", rv_no_token)

                    leg1_success = place_limit_buy_order(
                        clob_client,
                        rv_15m_leg[1],
                        RV_SHARES,
                        BUY_PRICE,
                        f"RV 15m {rv_15m_leg[0]}"
                    )
                    leg2_success = place_limit_buy_order(
                        clob_client,
                        rv_1h_leg[1],
                        RV_SHARES,
                        BUY_PRICE,
                        f"RV 1h {rv_1h_leg[0]}"
                    )

                    if leg1_success and leg2_success:
                        rv_open = True
                        rv_last_action_ts = now_ts
                    else:
                        log_message("RV open incomplete; skipping state change.")

                elif rv_open and not cooldown_active and abs(signal) <= RV_CLOSE_THR:
                    log_message("--- RV CLOSE ---")
                    if rv_15m_leg and rv_1h_leg:
                        close_15m_token = yes_token if rv_15m_leg[0] == "NO" else no_token
                        close_1h_token = rv_yes_token if rv_1h_leg[0] == "NO" else rv_no_token

                        leg1_success = place_limit_buy_order(
                            clob_client,
                            close_15m_token,
                            RV_SHARES,
                            BUY_PRICE,
                            "RV 15m CLOSE"
                        )
                        leg2_success = place_limit_buy_order(
                            clob_client,
                            close_1h_token,
                            RV_SHARES,
                            BUY_PRICE,
                            "RV 1h CLOSE"
                        )

                        if leg1_success and leg2_success:
                            rv_open = False
                            rv_last_action_ts = now_ts
                            rv_15m_leg = None
                            rv_1h_leg = None
                        else:
                            log_message("RV close incomplete; keeping position state.")

            time.sleep(CHECK_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            log_message(f"Loop Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
