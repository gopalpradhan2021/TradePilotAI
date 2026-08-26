import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class RiskConfig:
    max_order_value_inr: float
    max_daily_loss_inr: float
    max_trades_per_day: int
    max_position_qty: int
    price_sanity_band_pct: float
    total_capital_inr: float
    allow_fno: bool


@dataclass(frozen=True)
class Settings:
    mode: str
    risk: RiskConfig
    ntfy_topic: str


def _float_env(key: str, default: float) -> float:
    val = os.getenv(key)
    return float(val) if val else default


def _int_env(key: str, default: int) -> int:
    val = os.getenv(key)
    return int(val) if val else default


def load_settings() -> Settings:
    risk = RiskConfig(
        max_order_value_inr=_float_env("MAX_ORDER_VALUE_INR", 5000),
        max_daily_loss_inr=_float_env("MAX_DAILY_LOSS_INR", 2000),
        max_trades_per_day=_int_env("MAX_TRADES_PER_DAY", 10),
        max_position_qty=_int_env("MAX_POSITION_QTY", 50),
        price_sanity_band_pct=_float_env("PRICE_SANITY_BAND_PCT", 3.0),
        total_capital_inr=_float_env("TOTAL_CAPITAL_INR", 20000),
        allow_fno=os.getenv("ALLOW_FNO", "false").strip().lower() == "true",
    )
    return Settings(
        mode=os.getenv("MODE", "PAPER").upper(),
        risk=risk,
        ntfy_topic=os.getenv("NTFY_TOPIC", ""),
    )
