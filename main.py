"""Main entry point for running TradingAgents directly via Python."""

from tradingagents import DEFAULT_CONFIG, TradingAgentsGraph


def main():
    # DEFAULT_CONFIG automatically applies TRADINGAGENTS_* environment variable overrides
    # (llm_provider, deep_think_llm, quick_think_llm, backend_url, etc.)
    config = DEFAULT_CONFIG.copy()

    # Initialize graph
    agent = TradingAgentsGraph(debug=True, config=config)

    # Run analysis for an asset
    ticker = "NVDA"
    trade_date = "2026-09-01"
    print(f"Propagating analysis for {ticker} on {trade_date}...")
    final_state, decision = agent.propagate(ticker, trade_date)

    print("\n--- Decision Result ---")
    print(decision)
    return final_state, decision


if __name__ == "__main__":
    main()
