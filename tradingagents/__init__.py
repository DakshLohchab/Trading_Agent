"""TradingAgents: multi-agent LLM financial trading framework."""

__version__ = "0.7.1"

from dotenv import find_dotenv, load_dotenv

# Load .env at package import so DEFAULT_CONFIG's env-var overlay and every LLM
# client see the user's keys whichever entry point started the process.
load_dotenv(find_dotenv(usecwd=True))
load_dotenv(find_dotenv(".env.enterprise", usecwd=True), override=False)

from .agents.analysts import AnalystType, AssetType, detect_asset_type, filter_analysts_for_asset_type
from .default_config import DEFAULT_CONFIG
from .graph.trading_graph import TradingAgentsGraph

__all__ = [
    "__version__",
    "AnalystType",
    "AssetType",
    "BinanceSpotClient",
    "CryptoDayTrader",
    "DEFAULT_CONFIG",
    "TradingAgentsGraph",
    "detect_asset_type",
    "filter_analysts_for_asset_type",
]


def __getattr__(name: str):
    if name == "CryptoDayTrader":
        from .intraday.crypto_day_trader import CryptoDayTrader
        return CryptoDayTrader
    if name == "BinanceSpotClient":
        from .intraday.data_sources.binance_spot import BinanceSpotClient
        return BinanceSpotClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
