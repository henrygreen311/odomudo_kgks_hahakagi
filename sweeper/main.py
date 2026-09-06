import asyncio
import logging
from config import Config
from eth_sweeper import EthSweeper
from sol_sweeper import SolSweeper

logging.basicConfig(level=logging.INFO, format='%(message)s')
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

async def main():
    try:
        Config.validate()
    except ValueError as e:
        print(f"Config error: {e}")
        return

    eth = EthSweeper()
    sol = SolSweeper()

    last_eth_balance = 0
    last_sol_balance = 0

    while True:
        try:
            print("============================")

            eth_balance = await eth.get_balance()
            sol_balance = await sol.get_balance()

            eth_eth = eth_balance / 1e18
            sol_sol = sol_balance / 1e9

            print(f"ETH Balance = {eth_eth:.8f}")
            print(f"SOL Balance = {sol_sol:.8f}")

            eth_triggered = False
            sol_triggered = False

            if eth_balance > last_eth_balance:
                if eth_balance >= eth.threshold_wei:
                    print(f"→ ETH threshold met ({Config.ETH_SWEEP_THRESHOLD} ETH) – sweeping...")
                    await eth.sweep()
                    eth_triggered = True
                else:
                    print(f"→ ETH below threshold ({Config.ETH_SWEEP_THRESHOLD} ETH)")
                last_eth_balance = eth_balance

            if sol_balance > last_sol_balance:
                if sol_balance >= sol.threshold_lamports:
                    print(f"→ SOL threshold met ({Config.SOL_SWEEP_THRESHOLD} SOL) – sweeping...")
                    await sol.sweep()
                    sol_triggered = True
                else:
                    print(f"→ SOL below threshold ({Config.SOL_SWEEP_THRESHOLD} SOL)")
                last_sol_balance = sol_balance

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