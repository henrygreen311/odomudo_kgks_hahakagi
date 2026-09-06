import asyncio
import logging
import json
import os
from config import Config
from eth_sweeper import EthSweeper
from sol_sweeper import SolSweeper
from telegram import send_log

BALANCES_FILE = "last_balances.json"

logging.basicConfig(level=logging.INFO, format='%(message)s')
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

def load_last_balances():
    """Load last known balances from file. Returns (eth_wei, sol_lamports) or (None, None)."""
    if os.path.exists(BALANCES_FILE):
        try:
            with open(BALANCES_FILE, "r") as f:
                data = json.load(f)
                return data.get("eth", 0), data.get("sol", 0)
        except:
            return None, None
    return None, None

def save_last_balances(eth_wei, sol_lamports):
    """Save current balances to file."""
    with open(BALANCES_FILE, "w") as f:
        json.dump({"eth": eth_wei, "sol": sol_lamports}, f)

async def main():
    try:
        Config.validate()
    except ValueError as e:
        print(f"Config error: {e}")
        return

    eth = EthSweeper()
    sol = SolSweeper()

    # Load last known balances
    last_eth_balance, last_sol_balance = load_last_balances()
    first_run = (last_eth_balance is None or last_sol_balance is None)

    # If first run, set last balances to current to avoid alerting on existing balance
    if first_run:
        print("First run – initializing balance tracker without sending alerts.")
        eth_balance = await eth.get_balance()
        sol_balance = await sol.get_balance()
        last_eth_balance = eth_balance
        last_sol_balance = sol_balance
        save_last_balances(last_eth_balance, last_sol_balance)
    else:
        # Convert to int (they are stored as int)
        last_eth_balance = int(last_eth_balance)
        last_sol_balance = int(last_sol_balance)

    while True:
        try:
            print("============================")

            eth_balance = await eth.get_balance()
            sol_balance = await sol.get_balance()

            eth_eth = eth_balance / 1e18
            sol_sol = sol_balance / 1e9

            print(f"ETH Balance = {eth_eth:.8f}")
            print(f"SOL Balance = {sol_sol:.8f}")

            # Send alert ONLY if balance increased from the stored last value
            if eth_balance > last_eth_balance:
                msg = f"ETH balance increased to {eth_eth:.8f} ETH"
                print(f"  {msg}")
                if Config.TELEGRAM_ENABLED:
                    await send_log(Config.TELEGRAM_TOKEN, Config.TELEGRAM_CHAT_ID, msg)
            if sol_balance > last_sol_balance:
                msg = f"SOL balance increased to {sol_sol:.8f} SOL"
                print(f"  {msg}")
                if Config.TELEGRAM_ENABLED:
                    await send_log(Config.TELEGRAM_TOKEN, Config.TELEGRAM_CHAT_ID, msg)

            eth_triggered = False
            sol_triggered = False

            if eth_balance > last_eth_balance:
                if eth_balance >= eth.threshold_wei:
                    print(f"→ ETH threshold met ({Config.ETH_SWEEP_THRESHOLD} ETH) – sweeping...")
                    await eth.sweep()
                    eth_triggered = True
                else:
                    print(f"→ ETH below threshold ({Config.ETH_SWEEP_THRESHOLD} ETH)")
                # Update last balance regardless of sweep
                last_eth_balance = eth_balance

            if sol_balance > last_sol_balance:
                if sol_balance >= sol.threshold_lamports:
                    print(f"→ SOL threshold met ({Config.SOL_SWEEP_THRESHOLD} SOL) – sweeping...")
                    await sol.sweep()
                    sol_triggered = True
                else:
                    print(f"→ SOL below threshold ({Config.SOL_SWEEP_THRESHOLD} SOL)")
                last_sol_balance = sol_balance

            # Update stored balances after processing
            save_last_balances(last_eth_balance, last_sol_balance)

            if not eth_triggered and not sol_triggered:
                print("Skipped – both below required thresholds.\n")
            else:
                print("")

            print("============================")

            await asyncio.sleep(Config.POLL_INTERVAL)

        except Exception as e:
            print(f"Error in main loop: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())