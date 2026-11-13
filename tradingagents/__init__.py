"""TradingAgents: multi-agent LLM financial trading framework."""

__version__ = "0.7.1"

from dotenv import find_dotenv, load_dotenv

# Load .env at package import so DEFAULT_CONFIG's env-var overlay and every LLM
# client see the user's keys whichever entry point started the process.
# usecwd=True walks from the working directory, so the installed console script
# finds the project's .env rather than looking beside site-packages. Values the
# caller has already exported are never overridden.
load_dotenv(find_dotenv(usecwd=True))
load_dotenv(find_dotenv(".env.enterprise", usecwd=True), override=False)

from .agents.analysts import AnalystType, AssetType, detect_asset_type, filter_analysts_for_asset_type
from .default_config import DEFAULT_CONFIG
from .graph.trading_graph import TradingAgentsGraph

__all__ = [
    "__version__",
    "AnalystType",
    "AssetType",
    "DEFAULT_CONFIG",
    "TradingAgentsGraph",
    "detect_asset_type",
    "filter_analysts_for_asset_type",
]
