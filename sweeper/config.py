class Config:
    SOURCE_SEED = "abandon about ability above able abstract absent absurd absorb access abuse accident"

    # Ethereum
    ETH_SOURCE_SEED = SOURCE_SEED
    ETH_TARGET_ADDRESS = "0x9Ecee50854E87855Db3F2cD87eaAaB8E40e92E7E"
    ETH_RPC_URL = "https://mainnet.infura.io/v3/c9ee7b4675dd43789f096ae9460ae69b"   # used for sending transactions (rare)
    ETH_CHAIN_ID = 1
    ETH_GAS_LIMIT = 21000
    ETH_GAS_PRICE_BUMP = 1.1
    ETH_SWEEP_THRESHOLD = 0.040

    ETHERSCAN_API_KEY = "Q8E74QZ94Y4DTMZKXH3F48C38UEGZQ95MQ"

    # Solana
    SOL_SOURCE_SEED = SOURCE_SEED
    SOL_TARGET_ADDRESS = "Ew2Rm5fVeN3GWzUapjdUEPRAErwBJDVHHc3avxY7WW1J"
    SOL_SWEEP_THRESHOLD = 0.90

    # General
    POLL_INTERVAL = 30

    @classmethod
    def validate(cls):
        missing = []
        for name in [
            "ETH_SOURCE_SEED",
            "ETH_TARGET_ADDRESS",
            "ETH_RPC_URL",
            "ETHERSCAN_API_KEY",
            "SOL_SOURCE_SEED",
            "SOL_TARGET_ADDRESS",
        ]:
            if not getattr(cls, name):
                missing.append(name)
        if missing:
            raise ValueError(f"Missing configuration values: {', '.join(missing)}")