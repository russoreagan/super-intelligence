"""Core trading advisor module implementing rule-based signal logic.

This module provides a TradingAdvisor class that generates buy/sell/hold signals
based on technical indicators: moving averages (golden/death cross) and RSI.
"""


class TradingAdvisor:
    """Rule-based trading advisor using technical indicators.
    
    Signals are generated based on:
    - Golden Cross (MA_short > MA_long): potential BUY
    - Death Cross (MA_short < MA_long): potential SELL
    - RSI > 70 (overbought): SELL signal
    - RSI < 30 (oversold): BUY signal
    """
    
    # RSI thresholds
    RSI_OVERBOUGHT = 70
    RSI_OVERSOLD = 30
    
    def __init__(self):
        """Initialize the TradingAdvisor."""
        pass
    
    def get_advice(self, symbol: str, price: float, ma_short: float, ma_long: float, rsi: float) -> str:
        """Generate buy/sell/hold advice based on technical indicators.
        
        Args:
            symbol: Asset ticker symbol (e.g., 'AAPL', 'BTC')
            price: Current price of the asset
            ma_short: Short-term moving average (e.g., 20-period)
            ma_long: Long-term moving average (e.g., 50-period)
            rsi: Relative Strength Index (0-100)
            
        Returns:
            One of 'BUY', 'SELL', or 'HOLD'
            
        Signal Logic:
            1. RSI Extremes (highest priority):
               - RSI > 70 (overbought) → SELL
               - RSI < 30 (oversold) → BUY
            2. Moving Average Crossovers (secondary):
               - Golden Cross (ma_short > ma_long) → BUY
               - Death Cross (ma_short < ma_long) → SELL
            3. Default → HOLD
        """
        
        # Rule 1: Check RSI overbought (SELL signal)
        if rsi > self.RSI_OVERBOUGHT:
            return 'SELL'
        
        # Rule 2: Check RSI oversold (BUY signal)
        if rsi < self.RSI_OVERSOLD:
            return 'BUY'
        
        # Rule 3: Check Golden Cross (ma_short > ma_long → BUY signal)
        if ma_short > ma_long:
            return 'BUY'
        
        # Rule 4: Check Death Cross (ma_short < ma_long → SELL signal)
        if ma_short < ma_long:
            return 'SELL'
        
        # Rule 5: Default to HOLD if no clear signal
        return 'HOLD'
