from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # On-chain data (Solana Tracker = primary trades/holders; RPC = funding tracing only)
    solana_tracker_api_key: str = Field(default="", alias="SOLANA_TRACKER_API_KEY")
    rpc_url: str = Field(default="", alias="RPC_URL")   # free Alchemy/Ankr Solana endpoint
    # Max cursor pages per get_token_trades call (~100 trades/page). Each page is
    # one request against the 200k/month budget — check api_usage before raising.
    trades_max_pages: int = Field(default=6, alias="TRADES_MAX_PAGES")
    # First N BC buys resolved to slot/block-index per graduation (free RPC).
    microstructure_first_n_buys: int = Field(default=50, alias="MICROSTRUCTURE_FIRST_N_BUYS")
    # Deprecated — Helius free tier exhausted; kept only as a fallback RPC if RPC_URL is blank
    helius_api_key: str = Field(default="", alias="HELIUS_API_KEY")

    # My wallet (optional — journal auto-ingest)
    my_wallet: str = Field(default="", alias="MY_WALLET")

    # Optional APIs
    bags_api_key: str = Field(default="", alias="BAGS_API_KEY")
    x_api_key: str = Field(default="", alias="X_API_KEY")

    # LLM
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    llm_provider: str = Field(default="ollama", alias="LLM_PROVIDER")
    llm_model: str = Field(default="llama3.1:8b", alias="LLM_MODEL")

    # Pump monitor tuning
    collection_window_seconds: int = Field(default=60, alias="COLLECTION_WINDOW_SECONDS")
    min_buyers_to_analyse: int = Field(default=3, alias="MIN_BUYERS_TO_ANALYSE")

    # Storage
    db_path: str = Field(default="./db/copilot.db", alias="DB_PATH")

    # Supabase (optional — enables dashboard sync; leave blank to run SQLite-only)
    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_service_key: str = Field(default="", alias="SUPABASE_SERVICE_KEY")

    # Telegram alerts (optional — leave blank to disable project-graduation alerts)
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # Server
    server_host: str = Field(default="0.0.0.0", alias="SERVER_HOST")
    server_port: int = Field(default=8000, alias="SERVER_PORT")


settings = Settings()
